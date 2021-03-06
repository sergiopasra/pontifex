#
# Copyright 2011 Universidad Complutense de Madrid
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

import logging
import json
import os.path
import shutil

from sqlalchemy import desc
from numina.recipes import DataFrame
from numina.pipeline import get_recipe
import yaml

from model import taskdir, datadir, productsdir, DataProduct, RecipeConfiguration


_logger = logging.getLogger("pontifex.proc")

def processPointing(session, **kwds):
    _logger.info('process called with kwds %s', kwds)
    _logger.info('creating root directory')

    basedir = str(kwds['id'])
    os.chdir(taskdir)
    _logger.info('root directory is %s', basedir)

    basedir = os.path.abspath(basedir)
    workdir = os.path.join(basedir, 'work')
    resultsdir = os.path.join(basedir, 'results')

    os.mkdir(basedir)
    os.mkdir(workdir)
    os.mkdir(resultsdir)

    os.chdir(basedir)

    _logger.info('create config files, put them in root dir')

    filename_json = os.path.join(resultsdir, 'task-control.json')
    filename_yaml = os.path.join(resultsdir, 'task-control.yaml')
    
    _logger.info('instrument=%(instrument)s mode=%(mode)s', kwds)
    try:
        recipeClass = get_recipe(kwds['instrument'], kwds['mode'])
    except ValueError:
        _logger.warning('cannot find entry point for %(instrument)s and %(mode)s', kwds)
        raise ValueError
        
    _logger.info('matching parameters')
    
    request = kwds['request']
    pset = request['pset']

    parameters = {}
    
    stored_parameters = session.query(RecipeConfiguration).filter_by(instrument_id=kwds['instrument'], 
                                        module='%s:%s' % (recipeClass.__module__, recipeClass.__name__),
                                        pset_name=pset,
                                        active=True).first()

    if stored_parameters is None:
        _logger.info('no stored parameters for this recipe')
        stored_parameters = {}

    for req in recipeClass.__requires__:
        _logger.info('recipe requires %s', req.name)
        _logger.info('default value is %s', req.value)
        if issubclass(req.value, DataFrame):
            # query here
            longname = '%s.%s' % (req.value.__module__, req.value.__name__)
            _logger.info('query for %s', longname)
            # FIXME: this query should be updated
            dps = session.query(DataProduct).filter_by(instrument_id=kwds['instrument'], 
                                                       datatype=longname, 
                                                       pset_name=pset).order_by(desc(DataProduct.id))

            _logger.info('checking context')
            for cdp in dps:
                if all((c in kwds['context']) for c in cdp.context):
                    _logger.info('found requirement with acceptable context: %s', cdp.reference)
                    break
            else:
                cdp = None

            if cdp is None:
                _logger.warning("can't find %s", longname)
                raise ValueError("can't find %s" % longname)
            else:
                parameters[req.name] = cdp.reference
                _logger.debug('copy %s', cdp.reference)
                shutil.copy(os.path.join(productsdir, cdp.reference), workdir)
        elif req.name in stored_parameters:
            _logger.info('parameter %s from stored parameters', req.name)
            parameters[req.name] = stored_parameters[req.name]
        else:
            _logger.info('parameter %s has default value')
            parameters[req.name] = req.value

    for req in recipeClass.__provides__:
        _logger.info('recipe provides %s', req)
    
    _logger.info('copying the frames')
    images = []
    for frame in kwds['frames']:
        _logger.debug('copy %s', frame.name)
        images.append(frame.name)
        shutil.copy(os.path.join(datadir, frame.name), workdir)

    _logger.info('copying the children results')
    children_results = []
    for child in kwds['children']:
        for rresult in child.rresult:
            for dp in rresult.data_product:
                _logger.debug('copy %s', dp.reference)
                children_results.append(dp.reference)
                shutil.copy(os.path.join(productsdir, dp.reference), workdir)

    config = {'observing_result': {'id': kwds['id'], 
        'frames': images,
        'children': children_results,
        'instrument': kwds['instrument'],
        'mode': kwds['mode'],
        }, 
        'reduction': {'recipe': recipeClass.__module__+':'+recipeClass.__name__, 'parameters': parameters, 'processing_set': pset},
        'instrument': kwds['ins_params'],
        }
    with open(filename_json, 'w+') as fp:
        json.dump(config, fp, indent=1)

    with open(filename_yaml, 'w+') as fp:
        yaml.dump(config, fp, indent=1)

    
    return 0

processMosaic = processPointing

processCollect = processPointing

