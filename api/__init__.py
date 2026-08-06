"""Web API 路由 handler 层。

按资源聚合为 mixin 类（MetaAPI / GroupsAPI / SessionsAPI / RunsAPI /
TestsetsAPI / EventsAPI / ConfRouteMixin），由 main.py 的 VirtualSessionPlugin
继承装配——handler 保持为 Star 实例上的 bound method，_ROUTES 的 getattr
解析与测试的 ``plugin.<handler>`` 调用方式均不变。_ROUTES 路由表集中于此，
main.py 注册时引用。
"""

from .common import MAX_SESSIONS_PER_GROUP, ConfRouteMixin
from .events import EventsAPI
from .groups import GroupsAPI
from .meta import MetaAPI
from .routes import _ROUTES
from .runs import RunsAPI
from .sessions import SessionsAPI
from .testsets import TestsetsAPI

__all__ = [
    "ConfRouteMixin",
    "MAX_SESSIONS_PER_GROUP",
    "EventsAPI",
    "GroupsAPI",
    "MetaAPI",
    "RunsAPI",
    "SessionsAPI",
    "TestsetsAPI",
    "_ROUTES",
]
