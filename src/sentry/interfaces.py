"""
sentry.interfaces
~~~~~~~~~~~~~~~~~

Interfaces provide an abstraction for how structured data should be
validated and rendered.

:copyright: (c) 2010-2012 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""

import itertools
import urlparse

from django.http import QueryDict
from django.utils.translation import ugettext_lazy as _

from sentry.app import env
from sentry.models import UserOption
from sentry.web.helpers import render_to_string


_Exception = Exception


def unserialize(klass, data):
    value = object.__new__(klass)
    value.__setstate__(data)
    return value


def get_context(lineno, context_line, pre_context=None, post_context=None):
    lineno = int(lineno)
    context = []
    start_lineno = lineno - len(pre_context or [])
    if pre_context:
        start_lineno = lineno - len(pre_context)
        at_lineno = start_lineno
        for line in pre_context:
            context.append((at_lineno, line))
            at_lineno += 1
    else:
        start_lineno = lineno
        at_lineno = lineno

    context.append((at_lineno, context_line))
    at_lineno += 1

    if post_context:
        for line in post_context:
            context.append((at_lineno, line))
            at_lineno += 1

    return context


class Interface(object):
    """
    An interface is a structured represntation of data, which may
    render differently than the default ``extra`` metadata in an event.
    """

    score = 0

    def __init__(self, **kwargs):
        self.attrs = kwargs.keys()
        self.__dict__.update(kwargs)

    def __setstate__(self, data):
        kwargs = self.unserialize(data)
        self.attrs = kwargs.keys()
        self.__dict__.update(kwargs)

    def __getstate__(self):
        return self.serialize()

    def unserialize(self, data):
        return data

    def serialize(self):
        return dict((k, self.__dict__[k]) for k in self.attrs)

    def get_composite_hash(self, interfaces):
        return self.get_hash()

    def get_hash(self):
        return []

    def to_html(self, event):
        return ''

    def to_string(self, event):
        return ''

    def get_title(self):
        return _(self.__class__.__name__)

    def get_search_context(self, event):
        """
        Returns a dictionary describing the data that should be indexed
        by the search engine. Several fields are accepted:

        - text: a list of text items to index as part of the generic query
        - filters: a map of fields which are used for precise matching
        """
        return {
            # 'text': ['...'],
            # 'filters': {
            #     'field": ['...'],
            # },
        }


class Message(Interface):
    """
    A standard message consisting of a ``message`` arg, and an optional
    ``params`` arg for formatting.

    If your message cannot be parameterized, then the message interface
    will serve no benefit.

    >>> {
    >>>     "message": "My raw message with interpreted strings like %s",
    >>>     "params": ["this"]
    >>> }
    """

    def __init__(self, message, params=()):
        self.message = message
        self.params = params

    def serialize(self):
        return {
            'message': self.message,
            'params': self.params,
        }

    def get_hash(self):
        return [self.message]

    def get_search_context(self, event):
        if isinstance(self.params, (list, tuple)):
            params = list(self.params)
        elif isinstance(self.params, dict):
            params = self.params.values()
        else:
            params = ()
        return {
            'text': [self.message] + params,
        }


class Query(Interface):
    """
    A SQL query with an optional string describing the SQL driver, ``engine``.

    >>> {
    >>>     "query": "SELECT 1"
    >>>     "engine": "psycopg2"
    >>> }
    """

    def __init__(self, query, engine=None):
        self.query = query
        self.engine = engine

    def get_hash(self):
        return [self.query]

    def serialize(self):
        return {
            'query': self.query,
            'engine': self.engine,
        }

    def get_search_context(self, event):
        return {
            'text': [self.query],
        }


class Stacktrace(Interface):
    """
    A stacktrace contains a list of frames, each with various bits (most optional)
    describing the context of that frame. Frames should be sorted with the most recent
    caller being the last in the list.

    The stacktrace contains one element, ``frames``, which is a list of hashes. Each
    hash must contain **at least** the ``filename`` attribute. The rest of the values
    are optional, but recommended.

    Each frame must contain the following attributes:

    ``filename``
      The relative filepath to the call

    The following additional attributes are supported:

    ``lineno``
      The lineno of the call
    ``abs_path``
      The absolute path to filename
    ``function``
      The name of the function being called
    ``module``
      Platform-specific module path (e.g. sentry.interfaces.Stacktrace)
    ``context_line``
      Source code in filename at lineno
    ``pre_context``
      A list of source code lines before context_line (in order) -- usually [lineno - 5:lineno]
    ``post_context``
      A list of source code lines after context_line (in order) -- usually [lineno + 1:lineno + 5]
    ``in_app``
      Signifies whether this frame is related to the execution of the relevant code in this stacktrace. For example,
      the frames that might power the framework's webserver of your app are probably not relevant, however calls to
      the framework's library once you start handling code likely are.

    >>> {
    >>>     "frames": [{
    >>>         "abs_path": "/real/file/name.py"
    >>>         "filename": "file/name.py",
    >>>         "function": "myfunction",
    >>>         "vars": {
    >>>             "key": "value"
    >>>         },
    >>>         "pre_context": [
    >>>             "line1",
    >>>             "line2"
    >>>         ],
    >>>         "context_line": "line3",
    >>>         "lineno": 3,
    >>>         "in_app": true,
    >>>         "post_context": [
    >>>             "line4",
    >>>             "line5"
    >>>         ],
    >>>     }]
    >>> }

    """
    score = 1000

    def __init__(self, frames):
        self.frames = frames
        for frame in frames:
            # ensure we've got the correct required values
            assert 'filename' in frame

            # lineno should be an int
            if 'lineno' in frame:
                frame['lineno'] = int(frame['lineno'])

            # in_app should be a boolean
            if 'in_app' in frame:
                frame['in_app'] = bool(frame['in_app'])

    def _shorten(self, value, depth=1):
        if depth > 5:
            return type(value)
        if isinstance(value, dict):
            return dict((k, self._shorten(v, depth + 1)) for k, v in sorted(value.iteritems())[:100 / depth])
        elif isinstance(value, (list, tuple, set, frozenset)):
            return tuple(self._shorten(v, depth + 1) for v in value)[:100 / depth]
        elif isinstance(value, (int, long, float)):
            return value
        elif not value:
            return value
        return value[:100]

    def serialize(self):
        return {
            'frames': self.frames,
        }

    def get_composite_hash(self, interfaces):
        output = self.get_hash()
        if 'sentry.interfaces.Exception' in interfaces:
            output.append(interfaces['sentry.interfaces.Exception'].type)
        return output

    def get_hash(self):
        output = []
        for frame in self.frames:
            if frame.get('module'):
                output.append(frame['module'])
            else:
                output.append(frame['filename'])

            if frame.get('context_line'):
                output.append(frame['context_line'])
            elif frame.get('function'):
                output.append(frame['function'])
            elif frame.get('lineno'):
                output.append(frame['lineno'])
        return output

    def to_html(self, event):
        system_frames = 0
        frames = []
        for frame in self.frames:
            if frame.get('context_line') and frame.get('lineno') is not None:
                context = get_context(frame['lineno'], frame['context_line'], frame.get('pre_context'), frame.get('post_context'))
                start_lineno = context[0][0]
            else:
                context = []
                start_lineno = None

            context_vars = []
            if frame.get('vars'):
                context_vars = self._shorten(frame['vars'])
            else:
                context_vars = []

            if frame.get('lineno') is not None:
                lineno = int(frame['lineno'])
            else:
                lineno = None

            in_app = bool(frame.get('in_app', True))

            frames.append({
                'abs_path': frame.get('abs_path'),
                'filename': frame['filename'],
                'function': frame.get('function'),
                'start_lineno': start_lineno,
                'lineno': lineno,
                'context': context,
                'vars': context_vars,
                'in_app': in_app,
            })

            if not in_app:
                system_frames += 1

        if len(frames) == system_frames:
            system_frames = 0

        if env.request and env.request.user.is_authenticated():
            display = UserOption.objects.get_value(
                user=env.request.user,
                project=None,
                key='stacktrace_order',
                default=None,
            )
            if display == '2':
                frames.reverse()

        return render_to_string('sentry/partial/interfaces/stacktrace.html', {
            'system_frames': system_frames,
            'event': event,
            'frames': frames,
            'stacktrace': self.get_traceback(event),
        })

    def to_string(self, event):
        return self.get_stacktrace(event)

    def get_stacktrace(self, event):
        result = [
            'Stacktrace (most recent call last):', '',
        ]
        for frame in self.frames:
            pieces = ['  File "%(filename)s"']
            if 'lineno' in frame:
                pieces.append(', line %(lineno)s')
            if 'function' in frame:
                pieces.append(', in %(function)s')

            result.append(''.join(pieces) % frame)
            if 'context_line' in frame:
                result.append('    %s' % frame['context_line'].strip())

        return '\n'.join(result)

    def get_traceback(self, event):
        result = [
            event.message, '',
            self.get_stacktrace(event),
        ]

        return '\n'.join(result)

    def get_search_context(self, event):
        return {
            'text': list(itertools.chain(*[[f.get('filename'), f.get('function'), f.get('context_line')] for f in self.frames])),
        }


class Exception(Interface):
    """
    A standard exception with a mandatory ``value`` argument, and optional
    ``type`` and``module`` argument describing the exception class type and
    module namespace.

    >>>  {
    >>>     "type": "ValueError",
    >>>     "value": "My exception value",
    >>>     "module": "__builtins__"
    >>> }
    """

    score = 900

    def __init__(self, value, type=None, module=None):
        # A human readable value for the exception
        self.value = value
        # The exception type name (e.g. TypeError)
        self.type = type
        # Optional module of the exception type (e.g. __builtin__)
        self.module = module

    def serialize(self):
        return {
            'type': self.type,
            'value': self.value,
            'module': self.module,
        }

    def get_hash(self):
        return filter(bool, [self.type, self.value])

    def to_html(self, event):
        last_frame = None
        interface = event.interfaces.get('sentry.interfaces.Stacktrace')
        if interface is not None and interface.frames:
            last_frame = interface.frames[-1]
        return render_to_string('sentry/partial/interfaces/exception.html', {
            'event': event,
            'exception_value': self.value,
            'exception_type': self.type,
            'exception_module': self.module,
            'last_frame': last_frame
        })

    def get_search_context(self, event):
        return {
            'text': [self.value, self.type, self.module]
        }


class Http(Interface):
    """
    The Request information is stored in the Http interface. Two arguments
    are required: ``url`` and ``method``.

    The ``env`` variable is a compounded dictionary of HTTP headers as well
    as environment information passed from the webserver.

    The ``data`` variable should only contain the request body (not the query
    string). It can either be a dictionary (for standard HTTP requests) or a
    raw request body.

    >>>  {
    >>>     "url": "http://absolute.uri/foo",
    >>>     "method": "POST",
    >>>     "data": {
    >>>         "foo": "bar"
    >>>     },
    >>>     "query_string": "hello=world",
    >>>     "cookies": "foo=bar",
    >>>     "headers": {
    >>>         "Content-Type": "text/html"
    >>>     },
    >>>     "env": {
    >>>         "REMOTE_ADDR": "192.168.0.1"
    >>>     }
    >>>  }
    """

    score = 10000

    # methods as defined by http://www.w3.org/Protocols/rfc2616/rfc2616-sec9.html + PATCH
    METHODS = ('GET', 'POST', 'PUT', 'OPTIONS', 'HEAD', 'DELETE', 'TRACE', 'CONNECT', 'PATCH')

    def __init__(self, url, method=None, data=None, query_string=None, cookies=None, headers=None, env=None, **kwargs):
        if data is None:
            data = {}

        if method:
            method = method.upper()

        urlparts = urlparse.urlsplit(url)

        if not query_string:
            # define querystring from url
            query_string = urlparts.query

        elif query_string.startswith('?'):
            # remove '?' prefix
            query_string = query_string[1:]

        self.url = '%s://%s%s' % (urlparts.scheme, urlparts.netloc, urlparts.path)
        self.method = method
        self.data = data
        self.query_string = query_string
        if cookies:
            self.cookies = cookies
        else:
            self.cookies = {}
        # if cookies were [also] included in headers we
        # strip them out
        if headers and 'Cookie' in headers:
            cookies = headers.pop('Cookie')
            if cookies:
                self.cookies = cookies
        self.headers = headers or {}
        self.env = env or {}

    def serialize(self):
        return {
            'url': self.url,
            'method': self.method,
            'data': self.data,
            'query_string': self.query_string,
            'cookies': self.cookies,
            'headers': self.headers,
            'env': self.env,
        }

    def to_string(self, event):
        return render_to_string('sentry/partial/interfaces/http.txt', {
            'event': event,
            'full_url': '?'.join(filter(bool, [self.url, self.query_string])),
            'url': self.url,
            'method': self.method,
            'query_string': self.query_string,
        })

    def _to_dict(self, value):
        if value is None:
            value = {}
        if isinstance(value, dict):
            return True, value
        try:
            value = QueryDict(value)
        except _Exception:
            return False, value
        else:
            return True, value

    def to_html(self, event):
        data = self.data
        data_is_dict = False
        headers_is_dict, headers = self._to_dict(self.headers)

        if headers_is_dict and headers.get('Content-Type') == 'application/x-www-form-urlencoded':
            data_is_dict, data = self._to_dict(data)

        # It's kind of silly we store this twice
        cookies_is_dict, cookies = self._to_dict(self.cookies or headers.pop('Cookie', {}))

        return render_to_string('sentry/partial/interfaces/http.html', {
            'event': event,
            'full_url': '?'.join(filter(bool, [self.url, self.query_string])),
            'url': self.url,
            'method': self.method,
            'data': data,
            'data_is_dict': data_is_dict,
            'query_string': self.query_string,
            'cookies': cookies,
            'cookies_is_dict': cookies_is_dict,
            'headers': self.headers,
            'headers_is_dict': headers_is_dict,
            'env': self.env,
        })

    def get_search_context(self, event):
        return {
            'filters': {
                'url': [self.url],
            }
        }


class Template(Interface):
    """
    A rendered template (generally used like a single frame in a stacktrace).

    The attributes ``filename``, ``context_line``, and ``lineno`` are required.

    >>>  {
    >>>     "abs_path": "/real/file/name.html"
    >>>     "filename": "file/name.html",
    >>>     "pre_context": [
    >>>         "line1",
    >>>         "line2"
    >>>     ],
    >>>     "context_line": "line3",
    >>>     "lineno": 3,
    >>>     "post_context": [
    >>>         "line4",
    >>>         "line5"
    >>>     ],
    >>> }
    """

    score = 1001

    def __init__(self, filename, context_line, lineno, pre_context=None, post_context=None,
                 abs_path=None):
        self.abs_path = abs_path
        self.filename = filename
        self.context_line = context_line
        self.lineno = int(lineno)
        self.pre_context = pre_context
        self.post_context = post_context

    def serialize(self):
        return {
            'abs_path': self.abs_path,
            'filename': self.filename,
            'context_line': self.context_line,
            'lineno': self.lineno,
            'pre_context': self.pre_context,
            'post_context': self.post_context,
        }

    def get_hash(self):
        return [self.filename, self.context_line]

    def to_string(self, event):
        context = get_context(self.lineno, self.context_line, self.pre_context, self.post_context)
        result = [
            'Stacktrace (most recent call last):', '',
            self.get_traceback(event, context)
        ]

        return '\n'.join(result)

    def to_html(self, event):
        context = get_context(self.lineno, self.context_line, self.pre_context, self.post_context)

        return render_to_string('sentry/partial/interfaces/template.html', {
            'event': event,
            'abs_path': self.abs_path,
            'filename': self.filename,
            'lineno': int(self.lineno),
            'start_lineno': context[0][0],
            'context': context,
            'template': self.get_traceback(event, context),
        })

    def get_traceback(self, event, context):
        result = [
            event.message, '',
            'File "%s", line %s' % (self.filename, self.lineno), '',
        ]
        result.extend([n[1].strip('\n') for n in context])

        return '\n'.join(result)

    def get_search_context(self, event):
        return {
            'text': [self.abs_path, self.filename, self.context_line],
        }


class User(Interface):
    """
    An interface which describes the authenticated User for a request.

    All data is arbitrary and optional other than the ``is_authenticated``
    field which should be a boolean value indiciating whether the user
    is logged in or not.

    >>> {
    >>>     "is_authenticated": true,
    >>>     "id": "unique_id",
    >>>     "username": "foo",
    >>>     "email": "foo@example.com"
    >>> }
    """

    def __init__(self, is_authenticated, **kwargs):
        self.is_authenticated = is_authenticated
        self.id = kwargs.pop('id', None)
        self.username = kwargs.pop('username', None)
        self.email = kwargs.pop('email', None)
        self.data = kwargs

    def serialize(self):
        if self.is_authenticated:
            return {
                'is_authenticated': self.is_authenticated,
                'id': self.id,
                'username': self.username,
                'email': self.email,
                'data': self.data,
            }
        else:
            return {
                'is_authenticated': self.is_authenticated
            }

    def get_hash(self):
        return []

    def to_html(self, event):
        return render_to_string('sentry/partial/interfaces/user.html', {
            'event': event,
            'user_authenticated': self.is_authenticated,
            'user_id': self.id,
            'user_username': self.username,
            'user_email': self.email,
            'user_data': self.data,
        })

    def get_search_context(self, event):
        if not self.is_authenticated:
            return {}
        return {
            'text': [self.id, self.username, self.email]
        }
