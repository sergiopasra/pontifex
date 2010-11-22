from txrServer import txrServer
from dbins import session, datadir
from sql import ObsRun, ObsBlock, Images, lastindex

import datetime
import StringIO
import pyfits
import threading
from Queue import Queue
import logging
import logging.config
from xmlrpclib import Server
import os.path

logging.config.fileConfig("logging.conf")

# create logger
_logger = logging.getLogger("dbengine")

queue1 = Queue()
seqserver = Server('http://localhost:8010')

class DatabaseManager(object):
    
    def startobsrun(self, args):
        _logger.info('Received start observing run command')
        queue1.put(args)

    def startobsblock(self, args):
        _logger.info('Received start observing block command')
        queue1.put(args)

    def store_image(self, args):
        _logger.info('Received store image command')
        queue1.put(args)

    def endobsblock(self):
        _logger.info('Received end observing block command')
        queue1.put(('endobsblock',))

    def endobsrun(self):
        _logger.info('Received end observing run command')
        queue1.put(('endobsrun',))

    def version(self):
    	return '1.0'

im = DatabaseManager()

FORMAT = 's%05d.fits'

obsrun = None
ob = None

def store_image(ob, bindata, index):
    # Convert binary data back to HDUList
    handle = StringIO.StringIO(bindata)
    hdulist = pyfits.open(handle)
    # Write to disk
    filename = FORMAT % index
    hdulist.writeto(os.path.join(datadir, filename), clobber=True)
    # Update database
    img = Images(filename)
    img.exposure = hdulist[0].header['EXPOSED']
    img.imgtype = hdulist[0].header['IMGTYP']
    img.stamp = datetime.datetime.utcnow()
    ob.images.append(img)
    session.commit()

def manager():
    global queue1
    global obsrun
    global ob
    index = lastindex(session)
    _logger.info('Last stored image is number %d', index)
    _logger.info('Waiting for commands')
    while True:
        cmd = queue1.get()
        if cmd[0] == 'store':
            if ob is not None:
                _logger.info('Storing image %d', index)
                store_image(ob, cmd[1], index)
                index += 1
            else:
                _logger.warning('Observing block not initialized')
        elif cmd[0] == 'startobsrun':
            # Add ObsRun to database
            # startobsrun pidata
            _logger.info('Add ObsRun to database')
            obsrun = ObsRun(cmd[1])
            obsrun.start = datetime.datetime.utcnow()
            session.add(obsrun)
            session.commit()
            seqserver.obsrun_id(obsrun.runId)
        elif cmd[0] == 'endobsrun':
            if obsrun is not None:
                _logger.info('Update endtime of ObsRun in database')
                # endobssrun
                obsrun.end = datetime.datetime.utcnow()
                obsrun.status = 'FINISHED'
                session.commit()
                obsrun = None
        elif cmd[0] == 'startobsblock':
            # Add ObsBlock to database
            if obsrun is not None:
                _logger.info('Add ObsBlock to database')
                ob = ObsBlock(cmd[1], cmd[2])
                ob.start = datetime.datetime.utcnow()
                obsrun.obsblock.append(ob)
                session.commit()
            else:
                _logger.warning('Observing Run not iniatialized')
        elif cmd[0] == 'endobsblock':
            if ob is not None:
                _logger.info('Update endtime of ObsBlock in database')
                ob.end = datetime.datetime.utcnow()
                session.commit()    
                ob = None
            else:
                _logger.warning('Observing Block not iniatialized')
        else:
            _logger.warning('Command %s does not exist', cmd[0])


server = txrServer(('localhost', 8050), allow_none=True, logRequests=False)
server.register_instance(im)

server.register_function(server.shutdown, name='shutdown')

th = []
th.append(threading.Thread(target=manager))
th.append(threading.Thread(target=server.serve_forever))

for i in th:
    i.start()

for i in th:
    i.join()
