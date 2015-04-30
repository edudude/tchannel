# Copyright (c) 2015 Uber Technologies, Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

from __future__ import absolute_import

import tornado
import tornado.gen
from tornado import gen, ioloop
from ..handler import BaseRequestHandler
from ..messages.error import ErrorCode
from ..messages.common import StreamState, FlagsType
from .util import get_arg
from ..event import EventType
from ..zipkin.trace import Trace


class RequestDispatcher(BaseRequestHandler):
    """A synchronous RequestHandler that dispatches calls to different
    endpoints based on ``arg1``.

    Endpoints are registered using ``register`` or the ``route``
    decorator.

    .. code-block:: python

        handler = # ...

        @hanlder.route('myMethod')
        def myMethod(request, response, opts):
            response.write('hello world')
    """

    def __init__(self):
        super(RequestDispatcher, self).__init__()
        self.endpoints = {}

    @tornado.gen.coroutine
    def _call_endpoint(self, endpoint, request, response, proxy):
        try:
            yield tornado.gen.maybe_future(
                endpoint(request, response, proxy))
        except Exception:
            # TODO send application error
            pass
        finally:
            response.finish()

    @tornado.gen.coroutine
    def handle_call(self, request, connection):
        # read arg_1 so that handle_call is able to get the endpoint
        # name and find the endpoint handler.
        # the arg_1 value will be store in the request.endpoint field.

        # NOTE: after here, the correct way to access value of arg_1 is through
        # request.endpoint. The original argstream[0] is no longer valid. If
        # user still tries read from it, it will return empty.
        chunk = yield request.argstreams[0].read()
        while chunk:
            request.endpoint += chunk
            chunk = yield request.argstreams[0].read()

        # event: receive_request
        if connection.tchannel:
            request.tracing.name = request.endpoint
            connection.tchannel.event_emitter.fire(
                EventType.receive_request, request)

        endpoint = self.endpoints.get(request.endpoint, None)
        if endpoint is None:
            connection.send_error(
                ErrorCode.bad_request,
                "Endpoint '%s' for service '%s' is not defined" % (
                    request.endpoint, request.service),
                request.id,
            )
        else:
            response = Response(id=request.id,
                                tracing=request.tracing,
                                connection=connection)

            yield self._call_endpoint(endpoint, request, response,
                                      TChannelProxy(
                                          connection.tchannel,
                                          request.tracing))

    def route(self, rule):
        def decorator(handler):
            self.register(rule, handler)
            return handler

        return decorator

    def register(self, rule, handler):
        """Register a new endpoint with the given name.

        .. code-block:: python

            def handler(request, response, proxy):
                proxy.request(serviceName).send(...) # send outgoing request
                # ...

            handler.register('is_healthy', handler, foo='bar')

        :param rule:
            Name of the endpoint. Incoming Call Requests must have this as
            ``arg1`` to dispatch to this handler.
        :param handler:
            A function that gets called with ``Request``, ``Response``, and
            the ``proxy''.
        """
        assert rule, "rule must not be None"
        assert handler, "handler must not be None"
        self.endpoints[rule] = handler


class TornadoDispatcher(RequestDispatcher):
    """Dispatches requests to different endpoints based on ``arg1``"""

    def _call_endpoint(self, endpoint, request, response, proxy):
        future = gen.maybe_future(
            endpoint(request, response, proxy)
        )
        future.add_done_callback(lambda _: response.finish())

        # This is just to make sure that the Future gets consumed.
        ioloop.IOLoop.current().add_future(future, lambda f: f.exception())

        return future


class Request(object):
    """Represents an incoming request to an endpoint.

    Request class is used to represent the CallRequestMessage at User's level.
    This is going to hide the protocol level message information.
    """

    # TODO decide which elements inside "message" object to expose to user.
    def __init__(
        self,
        id=None,
        flags=FlagsType.none,
        ttl=10,
        tracing=None,
        service=None,
        headers=None,
        checksum=None,
        argstreams=None
    ):
        self.flags = flags
        self.ttl = ttl
        self.service = service
        self.tracing = tracing or Trace()
        # argstreams is a list of InMemStream/PipeStream objects
        self.argstreams = argstreams
        self.id = id
        self.headers = headers or {}
        self.state = StreamState.init
        self.endpoint = ""

    @property
    def arg_scheme(self):
        return self.headers.get('as', None)

    def close_argstreams(self, force=False):
        for stream in self.argstreams:
            if stream.auto_close or force:
                stream.close()

    def arg1(self):
        """get value for arg1

        :return: return the future object contains the value for arg1
        """
        return get_arg(self, 0)

    def arg2(self):
        """get value for arg2

        :return: return the future object contains the value for arg2
        """
        return get_arg(self, 1)

    def arg3(self):
        """get value for arg3

        :return: return the future object contains the value for arg3
        """
        return get_arg(self, 2)

    def args(self):
        """get value for arg1, arg2, and arg3

        :return: return the future object contains the list
        for arg1, arg2, arg3
        """
        return [self.arg1(), self.arg2(), self.arg3()]


class Response(object):
    """An outgoing response.

    Response class is used to represent the CallResponseMessage at User's
    level. This is going to hide the protocol level message information.
    """

    # TODO decide which elements inside "message" object to expose to user.
    def __init__(
        self,
        connection=None,
        flags=FlagsType.none,
        code=0,
        tracing=None,
        headers=None,
        checksum=None,
        argstreams=None,
        id=None
    ):

        self.flags = flags
        self.code = code
        self.tracing = tracing
        self.checksum = checksum
        # argstreams is a list of InMemStream/PipeStream objects
        self.argstreams = argstreams
        self.headers = headers
        self.id = id
        self.connection = connection
        self.state = StreamState.init

    def finish(self):
        """Finish writing the response."""
        # TODO verify the argstream contains valid objects and size
        self.connection.post_response(self)

    def close_argstreams(self, force=False):
        for stream in self.argstreams:
            if stream.auto_close or force:
                stream.close()

    def arg1(self):
        """get value for arg1

        :return: return the future object contains the value for arg1
        """
        return get_arg(self, 0)

    def arg2(self):
        """get value for arg2

        :return: return the future object contains the value for arg2
        """
        return get_arg(self, 1)

    def arg3(self):
        """get value for arg3

        :return: return the future object contains the value for arg3
        """
        return get_arg(self, 2)

    def args(self):
        """get value for arg1, arg2, and arg3

        :return: return the future object contains the list
        for arg1, arg2, arg3
        """
        return [self.arg1(), self.arg2(), self.arg3()]


class TChannelProxy(object):
    """TChannel Proxy with additional runtime info

    TChannelProxy contains parent_tracing information which is created by
    received request.

    TChannelProxy will be used as one parameter for the request handler.

    Example::

        def handler(request, response, proxy):

    """
    __slots__ = ('_tchannel', 'parent_tracing')

    def __init__(self, tchannel, parent_tracing=None):
        self._tchannel = tchannel
        self.parent_tracing = parent_tracing

    @property
    def closed(self):
        return self._tchannel.closed

    @property
    def hostport(self):
        return self._tchannel.hostport

    def host(self, handler):
        return self._tchannel.host(handler)

    def listen(self):
        return self._tchannel.listen()

    def request(self, hostport=None, service=None, **kwargs):
        kwargs['parent_tracing'] = self.parent_tracing
        return self._tchannel.request(hostport,
                                      service,
                                      **kwargs)