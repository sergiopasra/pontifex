#
# Copyright 2011 Sergio Pascual
# 
# This file is part of Pontifex
# 
# Pontifex is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# Pontifex is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with Pontifex.  If not, see <http://www.gnu.org/licenses/>.
#

# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4

import time
import threading
import logging
from Queue import Queue
from xmlrpclib import ServerProxy
import os.path
from datetime import datetime
import signal
import sys
import uuid
import ConfigParser
import json
import shutil

from sqlalchemy import create_engine
from numina.user import run_recipe
from numina.jsonserializer import from_json

import pontifex.process as process
from pontifex.txrServer import txrServer
import pontifex.model as model
from pontifex.model import Session, productsdir
from pontifex.model import ObservingBlock
from pontifex.model import ContextDescription, ContextValue
from pontifex.model import DataProcessingTask, ReductionResult, DataProduct

# create logger
_logger_s = logging.getLogger("pontifex.server")

# Processing tasks STATES
CREATED, COMPLETED, ENQUEUED, PROCESSING, FINISHED, ERROR = range(6)

class PontifexServer(object):
    def __init__(self):
        super(PontifexServer, self).__init__()

        self.doned = False
        self.queue = Queue()
        self.qback = Queue()
        self.clientlock = threading.Lock()
        self.client_hosts = {}
        self.nclient_hosts = 0
        _logger_s.info('ready')

    def quit(self):
        _logger_s.info('ending')
        self.doned = True
        self.qback.put(None)
        self.queue.put(None)

    def version(self):
        return '1.0'

    def register(self, hostid, host, port, capabilities):
        with self.clientlock:
            if hostid not in self.client_hosts:
                self.nclient_hosts += 1
                self.client_hosts[hostid]= [ServerProxy('http://%s:%d' % (host, port)), (host, port), capabilities, True]
                _logger_s.info('host registered %s %s:%d %s', hostid, host, port, capabilities)

    def unregister(self, hostid):
        with self.clientlock:
            _logger_s.info('unregistering host %s', hostid)
            self.nclient_hosts -= 1
            del self.client_hosts[hostid]


    def find_client(self, session, task):
        _logger_s.info('finding host for task=%d', task.id)
        for idx in self.client_hosts:
            server, (host, port), _, idle = self.client_hosts[idx]
            if idle:

                task.state = PROCESSING
                task.host = '%s:%d' % (host, port)
                _logger_s.info('sending to host %s', task.host)
                session.commit()
                server.pass_info(task.id)
                with self.clientlock:
                    self.nclient_hosts -= 1
                    self.client_hosts[idx][3] = False
                return idx
        else:
            _logger_s.info('no server for taskid=%d', task.id)
        
        return None

    def watchdog(self, pollfreq):
        session_w = Session()
        while True:
            if self.doned:
                _logger_s.info('cleaning up pending jobs')
                for task in session_w.query(DataProcessingTask).filter_by(state=ENQUEUED):
                    task.state = COMPLETED
                session_w.commit()
                _logger_s.info('watchdog finished')
                return
            else:            
                time.sleep(pollfreq)                
                for task in session_w.query(DataProcessingTask).filter_by(state=COMPLETED, waiting=False)[:self.nclient_hosts]:
                    _logger_s.info('enqueueing task %d ', task.id)
                    task.state = ENQUEUED
    
                    session_w.commit()
                    self.queue.put(task.id)

    def inserter(self):
        session_i = Session()
        # clean up on startup
        q = session_i.query(DataProcessingTask).filter_by(state=ENQUEUED)
        for i in q:
            _logger_s.info('fixing job %d', i.id)
            i.state = COMPLETED
        session_i.commit()

        while True:
            val = self.qback.get()
            if self.doned or val is None:
                _logger_s.info('inserter finished')
                return
            else:
                _, result, taskid = val
                _logger_s.info('updating done work, ProcessingTask %d', int(taskid))
                task = session_i.query(DataProcessingTask).filter_by(id=taskid).one() 

                task.completion_time = datetime.utcnow()

                results = {}

                if 'error' not in result:
                    task.state = FINISHED
                    
                    #cwd = os.getcwd()
                    os.chdir(os.path.abspath('results'))

                    # Update parent waiting state
                    _logger.debug('checking parent waiting state')
                    if task.parent is not None:
                        parent = task.parent
                        for child in parent.children:
                            if child.id == task.id:
                                # myself, ignoring
                                continue
                            if child.state != FINISHED:
                                break
                        else:
                            _logger.info('updating parent waiting state')
                            parent.waiting = False

                    results['control'] = ['task-control.json']
                    results['log'] = ['processing.log']
                    results['products'] = result['products']
                    
                    # FIXME: workaround to get instrument name
                    _logger.debug('workaround to get instrument name')
                    with open('task-control.json', 'r') as fd:
                        dic = json.load(fd)

                    iname = dic['instrument']['name']
                    del dic

                    task.result = str(results)
                    rr = ReductionResult()
                    rr.other = str(result)
                    rr.task_id = task.id
                    rr.obsres_id = task.obstree_node_id

                    # processing data products
                    for pr in result['products']:
                        prod = from_json(pr)
                
                        dp = DataProduct()
                        dp.instrument_id = iname
                        dp.datatype = '%s.%s' % (prod.__class__.__module__, prod.__class__.__name__)
                        # FIXME: this is specific for FITS files (classes that subclass Image)
                        dp.reference = prod.filename
                        dp.result = rr
                        
                        _logger.debug('extracting meta')
                        for key, val in prod.metadata():
                            _logger.debug('metadata is (%s, %s)', key, val)
                            # FIXME: probably there is a better way of doing this
                            q = session_i.query(ContextDescription).filter_by(instrument_id=dp.instrument_id, name=key).first()
                            v = session_i.query(ContextValue).filter_by(definition=q, value=val).first()

                            if v is None:
                                _logger.debug('creating metadata for %s', key)
                                v = ContextValue()
                                v.definition[q.together] = q
                                v.value = val
                                session_i.add(v)
                            
                            dp.context.append(v)

                        # copy or hardlink the file
                        _logger.debug('copying product in %s', productsdir)
                        # FIXME: no description
                        shutil.copy(prod.filename, productsdir)
                        # in 'products'
                        dp.task = task
                        session_i.add(dp)

                    session_i.add(rr)
                else:
                    results['error'] = result['error']
                    _logger.warning('error in task %d', task.id)
                    _logger.warning('error is %s', results['error']['type'])
                    _logger.warning('message is %s', results['error']['message'])
                    task.result = str(results)
                    task.state = ERROR

                session_i.commit()
                self.qback.task_done()

    def consumer(self):
        session = Session()
        while True:
            taskid = self.queue.get()
            if self.doned or taskid is None:
                _logger_s.info('consumer is finished')
                return
            else:
                task = session.query(DataProcessingTask).filter_by(id=taskid).first()
                task.start_time = datetime.utcnow()

                assert(task.state == ENQUEUED)
                try:                    
                    kwds = {}
                    kwds['id'] = task.id
                    kwds['children'] = task.children
                    kwds['images'] = task.obstree_node.images
                    kwds['mode'] = task.obstree_node.mode
                    
                    # finding parent node
                    # FIXME: find a better way of doing this:
                    # Recover the instrument of the task
                    otask = task
                    while(otask.parent):
                        otask = otask.parent
                                        
                    ob = otask.obstree_node.observing_block
                    
                    kwds['instrument'] = ob.obsrun.instrument_id
                    kwds['context'] = task.obstree_node.context

                    fun = getattr(process, task.method)
                    val = fun(session, **kwds)
                except Exception as ex:
                    task.completion_time = datetime.utcnow()
                    task.state = ERROR
                    _logger_s.warning('error creating root for task %d', taskid)
                    _logger_s.warning('error is %s', ex)
                    session.commit()
                    continue

                cid = self.find_client(session, task)
                if cid is not None:
                    _logger_s.info('processing taskid %d in host %s', taskid, cid)
                else:
                    self.queue.task_done()                    
                    self.qback.put((0, 1, task.id))
                session.commit()

    def receiver(self, cid, result, taskid):
        self.queue.task_done()
        self.qback.put((cid, result, taskid))
        with self.clientlock:
            self.nclient_hosts += 1
            self.client_hosts[cid][3] = True

    def run(self, obsid):
        '''Insert a new processing task tree in the database.'''

        _logger.info('create a new task tree for obsid %d', obsid)
        session = Session()

        def create_reduction_tree(otask, rparent):
            '''Climb the tree and create DataProcessingTask in nodes.'''
            rtask = DataProcessingTask()
            rtask.parent = rparent
            rtask.obstree_node = otask
            rtask.creation_time = datetime.utcnow()
            if otask.state == 2:
                rtask.state = COMPLETED
            else:
                rtask.state = CREATED
            rtask.method = 'process%s' % otask.label.capitalize()

            if otask.children:
                rtask.waiting = True
            else:
                rtask.waiting = False

            session.add(rtask)

            for child in otask.children:
                create_reduction_tree(child, rtask)
            
            return rtask

        
        obsblock = session.query(ObservingBlock).filter_by(id=obsid).first()
        _logger.info('observing tasks tree')
        
        rtask = create_reduction_tree(obsblock.observing_tree, None)
        _logger.info('new root processing task is %d', rtask.id)
        session.commit()

# create logger for host
_logger = logging.getLogger("pontifex.host")

class PontifexHost(object):
    def __init__(self, master, host, port):
        super(PontifexHost, self).__init__()
        uid = uuid.uuid5(uuid.NAMESPACE_URL, 'http://%s:%d' % (host, port))
        self.cid = uid.hex
        self.host = host
        self.port = port
        self.rserver = ServerProxy(master)
        self.rserver.register(self.cid, host, port, ['clodia'])

        self.doned = False
        self.queue = Queue()

        _logger.info('ready')

    def quit(self):
        _logger.info('ending')
        self.rserver.unregister(self.cid)
        self.queue.put(None)

    def version(self):
        return '1.0'

    def pass_info(self, taskid):
        _logger.info('received taskid=%d', taskid)
        self.queue.put(taskid)

    def worker(self):
        taskdir = os.path.abspath('task')
        while True:
            taskid = self.queue.get()            
            if taskid is not None:
                _logger.info('processing taskid %d', taskid)
                basedir = os.path.join(taskdir, str(taskid))
                workdir = os.path.join(basedir, 'work')
                resultsdir = os.path.join(basedir, 'results')
                filename = os.path.join(resultsdir, 'task-control.json')
                _logger.debug('%s', basedir)
                _logger.debug('%s', workdir)
                _logger.debug('%s', resultsdir)
                result = run_recipe(filename, workdir=workdir, 
                                    resultsdir=resultsdir, cleanup=False)

                _logger.info('finished')
                
                self.queue.task_done()
                _logger.info('sending to server')
                self.rserver.receiver(self.cid, result, taskid)
                os.chdir(taskdir)
            else:
                _logger.info('ending worker thread')
                return

def main_cli():

    masterurl = 'http://127.0.0.1:7081'

    rserver = ServerProxy(masterurl)

    def run(*args):
        rserver.run(args[0])

    def usage(args, parser):
        parser.print_help()

    import argparse

    parser = argparse.ArgumentParser(description='Pontifex command line utility',
                                     prog='pontifex',
                                     epilog="For detailed help pass " \
                                               "--help to a target")

    # Verbosity
    parser.add_argument('-v', action='store_true',
                        help='Run with verbose debug output')
    parser.add_argument('-q', action='store_true',
                        help='Run quietly only displaying errors')

    # Add a subparsers object to use for the actions
    subparsers = parser.add_subparsers(title='Targets',
                                       description='These are valid commands you can ask pontifex to do.')

    # Set up the various actions
    # Add help to -h and --help
    parser_help = subparsers.add_parser('help', help='Show usage')
    parser_help.set_defaults(command=lambda args: usage(args, parser=parser))

    # Add a common parser to be used as a parent
    parser_build_common = subparsers.add_parser('common',
                                                add_help=False)
    # build target
    parser_build = subparsers.add_parser('run',
                                         help='Request reduction',
                                         parents=[parser_build_common],
                                         description='This command \
                                         requests a reduction of a particular \
                                         observing block to be performed.')

    parser_build.add_argument('id', action='store', type=int,
                              help='Id of the observing block')

    parser_build.set_defaults(command=run)
    
    val = parser.parse_args()

    val.command(val.id)

def main_host():

    if len(sys.argv) != 2:
        sys.exit(1)

    cfgfile = sys.argv[1]

    config = ConfigParser.ConfigParser()
    config.read(cfgfile)

    masterurl = config.get('master', 'url')
    host = config.get('slave', 'host')
    port = config.getint('slave', 'port')

    im = PontifexHost(masterurl, host, port)

    tserver = txrServer((host, port), allow_none=True, logRequests=False)
    tserver.register_function(im.pass_info)

    # signal handler
    def handler(signum, frame):
        im.quit()
        tserver.shutdown()
        im.doned = True
        sys.exit(0)

    # Set the signal handler on SIGTERM and SIGINT
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    xmls = threading.Thread(target=tserver.serve_forever)
    xmls.start()

    worker = threading.Thread(target=im.worker)
    worker.start()

    while not im.doned:
        signal.pause()

def main_server():

    logging.config.fileConfig("logging.ini")

    df_server = ServerProxy('http://127.0.0.1:7080')

    engine = create_engine('sqlite:///devdata.db', echo=False)
    #engine = create_engine('sqlite:///devdata.db', echo=True)
    engine.execute('pragma foreign_keys=on')

    model.init_model(engine)
    model.metadata.create_all(engine)
    session = model.Session()

    im = PontifexServer()

    tserver = txrServer(('localhost', 7081), allow_none=True, logRequests=False)
    tserver.register_function(im.register)
    tserver.register_function(im.unregister)
    tserver.register_function(im.receiver)
    tserver.register_function(im.version)
    tserver.register_function(im.run)


    # signal handler
    def handler(signum, frame):
        im.quit()
        tserver.shutdown()
        im.doned = True
        sys.exit(0)

    # Set the signal handler on SIGTERM and SIGINT
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    xmls = threading.Thread(target=tserver.serve_forever)
    xmls.start()

    POLL = 5
    _logger_s.info('polling database for new ProcessingTasks every %d seconds', POLL)
    timer = threading.Thread(target=im.watchdog, args=(POLL, ), name='timer')
    timer.start()

    inserter = threading.Thread(target=im.inserter, name='inserter')
    inserter.start()

    consumer = threading.Thread(target=im.consumer, name='consumer')
    consumer.start()

    while not im.doned:
        signal.pause()
