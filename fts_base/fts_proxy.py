# -*- coding: utf-8 -*-
##############################################################################
#
#    OpenERP, Open Source Management Solution
#    This module copyright (C) 2012 Therp BV (<http://therp.nl>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp.osv.orm import TransientModel, except_orm
from openerp.osv import fields
from openerp.tools import DEFAULT_SERVER_DATETIME_FORMAT
from openerp.tools.translate import _
from openerp import SUPERUSER_ID
from openerp.release import version_info
import openerp
# temporary workaround for lp:1031442
import cache_fixed_kwargs as tools
#this override creates a nicer view for the search results
clean_action_org=openerp.addons.web.controllers.main.clean_action
def clean_action(req, action, do_not_eval=False):
    global clean_action_org
    if openerp.release.version_info[0] <= 6:
        action=clean_action_org(req, action,do_not_eval)
    else:
        action=clean_action_org(req, action)
    if (action.get('type')=='ir.actions.act_window' and action.get('search_view_id') 
            and action.get('search_view_id')[1]=='fts_proxy.search'):
        action['flags']['selectable']=False
        action['flags']['addable']=False
        action['flags']['isClarkGable']=False
        action['flags']['deletable']=False
    return action
openerp.addons.web.controllers.main.clean_action=clean_action

from fts_base import fts_base_meta
from fts_base import fts_base
from datetime import datetime
import psycopg2
import logging
logger = logging.getLogger('fulltextsearch')

class fts_proxy(TransientModel):

    _name = 'fts.proxy'

    def _get_model_name(self, cr, uid, ids, name, arg, context):
        self.read(cr, uid, ids, ['model'])

    _columns = {
              'name': fields.char('Name', size=256, readonly=True),
              'text': fields.function(lambda *args,**kwargs: {}, string='Searchstring',
                                      type='text',
                                      fnct_search=lambda *args,**kwargs: {}, readonly=True),
              'model': fields.char('Model', size=256, translate=True, readonly=True),
              'model_name': fields.function(lambda self, cr, uid, ids, name, arg, context: 
                  dict([
                      (this['id'], self.pool.get('ir.model').name_search(
                          cr, uid, this['model'])[0][1]) 
                      for this in 
                      self.read(cr, uid, ids, ['model'])]), string='Type', 
                  type='char', readonly=True),
              'res_id': fields.integer('Res ID', readonly=True),
              'rank': fields.float('Rank', readonly=True),
              'summary': fields.text('Summary', readonly=True)
              }

    _order = 'rank DESC, model ASC'

    _fts_models = {}

    def __init__(self, pool, cr):

        fts_base.pool = pool

        return super(fts_proxy, self).__init__(pool, cr)

    @tools.cache()
    def search(self, cr, uid, args, offset=0, limit=None, order=None,
               context=None, count=False):

        if context is None:
            context={}

        searchstring = ''
        models = []

        for arg in args:
            if arg[0] == 'text' and arg[1] == 'ilike':
                searchstring = arg[2]
            if arg[0] == 'model' and arg[1] == '=':
                models.append(arg[2])

        if not searchstring:
            logger.debug('doing nothing because i got no search string')
            return []

        res = 0 if count else []

        if not context.get('fts_no_syntax_check') and not offset and not count:
            try:
                cr.execute("select to_tsquery('pg_catalog.simple', %s)", (searchstring,))
            except psycopg2.ProgrammingError:
                raise except_orm(_('Error'), 
                        _('You filled in an invalid searchstring!'))

        #TODO: if this search is limited, it is probably about scrolling and 
        #we have cached the results of the initial nonlimited search. So return 
        #that. Should look something like
        #if self.search.lookup(self, cr, args, 0, None, order, context, count))
        #and works only in openerp6.1

        logger.debug('offset: %s limit: %s order=%s count=%s' % (offset, limit,
            order, count))
        logger.debug('args ' + str(args))
        logger.debug('context ' + str(context))

        #TODO: context may contain info which document types are interesting
        #only call search for that ones in this case
        for search_plugin in fts_base_meta._plugins:

            if models and search_plugin._model not in models:
                continue

            logger.debug('plugin ' + str(search_plugin))
            res += search_plugin.search(cr, uid, args, order=order,
                                            context=context, count=count,
                                            searchstring=searchstring)
            logger.debug(res)
            logger.debug('finished')

        if count:
            return res

        #TODO: cache ids of results for scolling (offset > 0). that should use
        #some kind of hash over the search parameters
        return super(fts_proxy, self).search(cr, uid, [('id', 'in', res)],
                                             offset=offset,
                                             limit=limit, order=order,
                                             context=context)

    def open_document(self, cr, uid, ids, context=None):
        if not ids:
            return False
        this=self.read(cr, uid, ids[0], ['model', 'res_id'])
        return {
                'type': 'ir.actions.act_window',
                'res_model': this['model'],
                'view_type': 'form',
                'view_mode': 'form,tree',
                'res_id': this['res_id'],
                }

    def preview_document(self, cr, uid, ids, context=None):
        if not ids:
            return False
        this=self.read(cr, uid, ids[0], ['model', 'res_id'])
        return {
                'type': 'ir.actions.act_window',
                'res_model': this['model'],
                'view_type': 'form',
                'view_mode': 'page' if version_info[0] < 7 else 'form',
                'target': 'new',
                'res_id': this['res_id'],
                }

    def create_init_tsvector_cronjob(self, cr, uid, fts_object):

        fts_classname = (fts_object._model)

        self.pool.get('ir.cron').create(cr, SUPERUSER_ID,
            {
                'name': 'fulltextsearch init ' + fts_classname,
                'nextcall' : datetime.now().strftime(
                                                DEFAULT_SERVER_DATETIME_FORMAT),
                'model': self._name,
                'function': 'init_tsvector_cronjob',
                'args': "('" + fts_classname + "',)",
                'interval_type': False,
                'interval_number': False,
            })

    def recreate_search_index(self, cr, uid, fts_object):
        fts_object._drop_indexed_column_trigger(self.pool, cr)
        fts_object._create_indexed_column_trigger(self.pool, cr)
        self.pool.get('fts.proxy').create_init_tsvector_cronjob(cr, 
                uid, fts_object)

    def init_tsvector_cronjob(self, cr, uid, fts_classname, context=None):

        import logging
        logger = logging.getLogger('fts_cronjob')
        logger.info('looking for search plugin ' + fts_classname)

        for search_plugin in fts_base_meta._plugins:
            if (search_plugin._model) == fts_classname:

                logger.info('running _init_tsvector_column for '+fts_classname)
                search_plugin._init_tsvector_column(self.pool, cr)
                logger.info('finished _init_tsvector_column for '+fts_classname)
