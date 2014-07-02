import logging
import datetime

from sqlalchemy import *
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import relation, backref, class_mapper, joinedload
from sqlalchemy.orm.session import Session
from sqlalchemy.ext.declarative import declarative_base

from kallithea.lib.dbmigrate.migrate import *
from kallithea.lib.dbmigrate.migrate.changeset import *
from kallithea.lib.utils2 import str2bool

from kallithea.model.meta import Base
from kallithea.model import meta
from kallithea.lib.dbmigrate.versions import _reset_base, notify

log = logging.getLogger(__name__)


def upgrade(migrate_engine):
    """
    Upgrade operations go here.
    Don't create your own engine; bind migrate_engine to your metadata
    """
    _reset_base(migrate_engine)
    from kallithea.lib.dbmigrate.schema import db_2_0_0
    tbl = db_2_0_0.Setting.__table__
    settings_value = tbl.columns.app_settings_value
    settings_value.alter(type=String(4096))

    # issue fixups
    fixups(db_2_0_0, meta.Session)


def downgrade(migrate_engine):
    meta = MetaData()
    meta.bind = migrate_engine


def fixups(models, _SESSION):
    return
