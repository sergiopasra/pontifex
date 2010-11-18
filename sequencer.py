from txrServer import txrServer

import threading
from Queue import Queue
import logging
import logging.config
from xmlrpclib import Server

logging.config.fileConfig("logging.conf")

# create logger
_logger = logging.getLogger("sequencer")

queue = Queue()

instruments = ['test']

dbserver = Server('http://localhost:8050')
insserver = Server('http://localhost:9010')

class SequenceManager(object):
    def __init__(self):
        self._instruments = instruments

    # Console
    def run_command(self, args):
        _logger.info('Received console command %s', args)
        argslist = args.split()
        if argslist[0] in instruments:
    	    queue.put(('instrument',) + tuple(argslist))
            return True
        else:
            _logger.warning('No such instrument')
            return False

    def version(self):
    	return True

    # Instrument
    def return_image(self, cmd):
        _logger.info('Received instrument command % s', cmd)
        queue.put(cmd)
        return True

sm = SequenceManager()

def sequencer():
    global queue
    _logger.info('Waiting for commands')
    while True:
        cmd = queue.get()
        # This cmd comes from the console
        if cmd[0] == 'instrument':
            _logger.info('Observation instrument=%s mode=%s started', cmd[1], cmd[2])
            # Create obsblock
            try:
                insserver.command(cmd[1:])
            except Exception, ex:
                _logger.error('Error %s', ex)
        # This cmd comes from the instrument
        elif cmd[0] == 'startobsblock':
            dbserver.startobsblock(cmd)
        # This cmd comes from the instrument
        elif cmd[0] == 'storeob':
            dbserver.endobsblock()
        # This cmd comes from the instrument
        elif cmd[0] == 'store':
            _logger.info('Sending command to storage engine')
            dbserver.store_image(cmd)
        else:
            _logger.warning('Command %s does not exist', cmd[0])

server = txrServer(('localhost', 8010), allow_none=True, logRequests=False)
server.register_instance(sm)

server.register_function(server.shutdown, name='shutdown')

th = []
th.append(threading.Thread(target=sequencer))
th.append(threading.Thread(target=server.serve_forever))

for i in th:
    i.start()

for i in th:
    i.join()

