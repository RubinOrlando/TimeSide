#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2012 Paul Brossier <piem@piem.org>

# This file is part of TimeSide.

# TimeSide is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.

# TimeSide is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with TimeSide.  If not, see <http://www.gnu.org/licenses/>.


from timeside.core import Processor, implements, interfacedoc
from timeside.component import implements, abstract
from timeside.api import IEncoder
from timeside.tools import *

#from gst import _gst as gst
import pygst
pygst.require('0.10')
import gst

import gobject
gobject.threads_init()

# Streaming queue configuration
QUEUE_SIZE = 10
GST_APPSINK_MAX_BUFFERS = 10


class GstEncoder(Processor):
    implements(IEncoder)
    abstract()

    def __init__(self, output, streaming=False, overwrite=False):

        super(GstEncoder, self).__init__()

        if isinstance(output, basestring):
            import os.path
            if os.path.isdir(output):
                raise IOError("Encoder output must be a file, not a directory")
            elif os.path.isfile(output) and not overwrite:
                raise IOError("Encoder output %s exists, but overwrite set to False")
            self.filename = output
        else:
            self.filename = None
        self.streaming = streaming

        if not self.filename and not self.streaming:
            raise Exception('Must give an output')

        import threading
        self.end_cond = threading.Condition(threading.Lock())

        self.eod = False
        self.metadata = None
        self.num_samples = 0

        self._chunk_len = 0

    @interfacedoc
    def release(self):
        if hasattr(self, 'eod') and hasattr(self, 'mainloopthread'):
            self.end_cond.acquire()
            while not hasattr(self, 'end_reached'):
                self.end_cond.wait()
            self.end_cond.release()
        if hasattr(self, 'error_msg'):
            raise IOError(self.error_msg)

    def __del__(self):
        self.release()

    def start_pipeline(self, channels, samplerate):
        self.pipeline = gst.parse_launch(self.pipe)
        # store a pointer to appsrc in our encoder object
        self.src = self.pipeline.get_by_name('src')

        if self.streaming:
            import Queue
            self._streaming_queue = Queue.Queue(QUEUE_SIZE)
            # store a pointer to appsink in our encoder object
            self.app = self.pipeline.get_by_name('app')
            self.app.set_property('max-buffers', GST_APPSINK_MAX_BUFFERS)
            self.app.set_property("drop", False)
            self.app.set_property('emit-signals', True)
            self.app.connect("new-buffer", self._on_new_buffer_streaming)
            self.app.connect('new-preroll', self._on_new_preroll_streaming)

        srccaps = gst.Caps("""audio/x-raw-float,
            endianness=(int)1234,
            channels=(int)%s,
            width=(int)32,
            rate=(int)%d""" % (int(channels), int(samplerate)))
        self.src.set_property("caps", srccaps)
        self.src.set_property('emit-signals', True)
        self.src.set_property('num-buffers', -1)
        self.src.set_property('block', True)
        self.src.set_property('do-timestamp', True)

        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus.connect("message", self._on_message_cb)

        import threading
        class MainloopThread(threading.Thread):
            def __init__(self, mainloop):
                threading.Thread.__init__(self)
                self.mainloop = mainloop

            def run(self):
                self.mainloop.run()
        self.mainloop = gobject.MainLoop()
        self.mainloopthread = MainloopThread(self.mainloop)
        self.mainloopthread.start()

        # start pipeline
        self.pipeline.set_state(gst.STATE_PLAYING)

    def _on_message_cb(self, bus, message):
        t = message.type
        if t == gst.MESSAGE_EOS:

            if self.streaming:
                self._streaming_queue.put(gst.MESSAGE_EOS)

            self.end_cond.acquire()
            self.pipeline.set_state(gst.STATE_NULL)
            self.mainloop.quit()
            self.end_reached = True
            self.end_cond.notify()
            self.end_cond.release()

        elif t == gst.MESSAGE_ERROR:
            self.end_cond.acquire()
            self.pipeline.set_state(gst.STATE_NULL)
            self.mainloop.quit()
            self.end_reached = True
            err, debug = message.parse_error()
            self.error_msg = "Error: %s" % err, debug
            self.end_cond.notify()
            self.end_cond.release()

    def _on_new_buffer_streaming(self, appsink):
        #print 'pull-buffer'
        chunk = appsink.emit('pull-buffer')
        if chunk == gst.MESSAGE_EOS:
            print 'chunk is eos *************'
            raise TypeError
        else:
            self._chunk_len += len(chunk)
            print 'new buffer', self._chunk_len

        if appsink.get_property('eos'):
            print 'property EOS'
        #print 'put buffer in queue'
        self._streaming_queue.put(chunk)
        #print 'qsize : %d' % self._streaming_queue.qsize()
        #print 'put ok'

    def _on_new_preroll_streaming(self, appsink):
        print 'preroll'
        chunk = appsink.emit('pull-preroll')
        self._streaming_queue.put(chunk)

    @interfacedoc
    def process(self, frames, eod=False):
        self.eod = eod
        if eod:
            self.num_samples +=  frames.shape[0]
        else:
            self.num_samples += self.blocksize()
        buf = numpy_array_to_gst_buffer(frames, frames.shape[0],self.num_samples, self.samplerate())
        self.src.emit('push-buffer', buf)
        if self.eod:
            self.src.emit('end-of-stream')
        if self.streaming:
            pass #self.chunk = self.app.emit('pull-buffer')
        return frames, eod

    def get_stream_chunk(self):
        if self.streaming:
            #if not self.app.get_property('eos'):
            #print 'get chunk from queue'
            #print 'qsize : %d' % self._streaming_queue.qsize()
            chunk = self._streaming_queue.get(block=True)
            if  chunk == gst.MESSAGE_EOS:
                return None
            else:
                self._streaming_queue.task_done()
                return chunk

            print 'new buffer', self._chunk_len

        else:
            raise TypeError('function only available in streaming mode')

if __name__ == "__main__":
    # Run doctest from __main__ and unittest from test_analyzer_preprocessors
    from tests import test_encoding, test_transcoding
    from tests.unit_timeside import run_test_module
    run_test_module([test_encoding, test_transcoding])