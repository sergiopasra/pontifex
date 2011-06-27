#!/usr/bin/python

# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4

import sys
import time
import threading
import logging
import logging.config
from Queue import Queue
import hashlib
import datetime
import uuid
import ConfigParser
from xmlrpclib import ServerProxy, ProtocolError, Error

import gobject
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
    
from ptimer import PeriodicTimer
from txrServer import txrServer

logging.config.fileConfig("logging.conf")

# create logger
_logger = logging.getLogger("DF slave")


class DatafactorySlave(object):
    def __init__(self, loop, master, host, port):
        super(DatafactorySlave, self).__init__()
        uid = uuid.uuid5(uuid.NAMESPACE_URL, 'http://%s:%d' % (host, port))
        self.cid = uid.hex
        self.loop = loop
        self.rserver = ServerProxy(master)
        self.rserver.register(self.cid, 'http://%s' % host, port, ['megara'])

        self.timer = None
        self.repeat = 5
        self.doned = False
        self.queue = Queue()
        self.qback = Queue()
        self.images = 0

        _logger.info('Waiting for commands')
        self.slaves = {}


    def quit(self):
        _logger.info('Ending')
        self.rserver.unregister(self.cid)
        self.doned = True
        self.qback.put(None)
        self.queue.put(None)
        self.queue.put(None)
        self.loop.quit()

    def version(self):
    	return '1.0'

    def pass_info(self, pid, n, tr, i, workdir):
        _logger.info('Received observation number=%s, recipe=%s, instrument=%s', n, tr, i)
        self.queue.put((pid, n, tr, i, workdir))

    def worker(self):
        while True:
            v = self.queue.get()
            if v is not None:
                pid, oid, tr, i, workdir = v
                name = threading.current_thread().name
                _logger.info('Processing observation number=%s, recipe=%s, instrument=%s', oid, tr, i)
                _logger.info('on directory %s', workdir)
                time.sleep(20)
                _logger.info('Finished')
                self.queue.task_done()
                self.rserver.receiver(self.cid, pid, oid, workdir)
            else:
                _logger.info('Ending worker thread')
                return

loop = gobject.MainLoop()
gobject.threads_init()

if len(sys.argv) != 2:
    sys.exit(1)

cfgfile = sys.argv[1]

config = ConfigParser.ConfigParser()
config.read(cfgfile)

masterurl = config.get('master', 'url')
host = config.get('slave', 'host')
port = config.getint('slave', 'port')

im = DatafactorySlave(loop, masterurl, host, port)

tserver = txrServer((host, port), allow_none=True, logRequests=False)
tserver.register_function(im.pass_info)
xmls = threading.Thread(target=tserver.serve_forever)
xmls.start()

worker = threading.Thread(target=im.worker)
worker.start()
try:
    loop.run()
except KeyboardInterrupt:
    im.quit()
    tserver.shutdown()

