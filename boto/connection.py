# Copyright (c) 2006-2010 Mitch Garnaat http://garnaat.org/
# Copyright (c) 2010 Google
# Copyright (c) 2008 rPath, Inc.
# Copyright (c) 2009 The Echo Nest Corporation
# Copyright (c) 2010, Eucalyptus Systems, Inc.
# Copyright (c) 2011, Nexenta Systems Inc.
# All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish, dis-
# tribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the fol-
# lowing conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
# OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABIL-
# ITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT
# SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

#
# Parts of this code were copied or derived from sample code supplied by AWS.
# The following notice applies to that code.
#
#  This software code is made available "AS IS" without warranties of any
#  kind.  You may copy, display, modify and redistribute the software
#  code either by itself or as incorporated into your code; provided that
#  you do not remove any proprietary notices.  Your use of this software
#  code is at your own risk and you waive any claim against Amazon
#  Digital Services, Inc. or its affiliates with respect to your use of
#  this software code. (c) 2006 Amazon Digital Services, Inc. or its
#  affiliates.

"""
Handles basic connections to AWS
"""

from __future__ import with_statement
import base64
import errno
import httplib
import os
import Queue
import random
import re
import socket
import sys
import time
import urllib, urlparse
import xml.sax

import auth
import auth_handler
import boto
import boto.utils
import boto.handler
import boto.cacerts

from boto import config, UserAgent
from boto.exception import AWSConnectionError, BotoClientError, BotoServerError
from boto.provider import Provider
from boto.resultset import ResultSet

HAVE_HTTPS_CONNECTION = False
try:
    import ssl
    from boto import https_connection
    # Google App Engine runs on Python 2.5 so doesn't have ssl.SSLError.
    if hasattr(ssl, 'SSLError'):
        HAVE_HTTPS_CONNECTION = True
except ImportError:
    pass

try:
    import threading
except ImportError:
    import dummy_threading as threading

ON_APP_ENGINE = all(key in os.environ for key in (
    'USER_IS_ADMIN', 'CURRENT_VERSION_ID', 'APPLICATION_ID'))

PORTS_BY_SECURITY = { True: 443, False: 80 }

DEFAULT_CA_CERTS_FILE = os.path.join(
        os.path.dirname(os.path.abspath(boto.cacerts.__file__ )), "cacerts.txt")

class HostConnectionPool(object):

    """
    A pool of connections for one remote (host,is_secure).

    When connections are added to the pool, they are put into a
    pending queue.  The _mexe method returns connections to the pool
    before the response body has been read, so they connections aren't
    ready to send another request yet.  They stay in the pending queue
    until they are ready for another request, at which point they are
    returned to the pool of ready connections.

    The pool of ready connections is an ordered list of
    (connection,time) pairs, where the time is the time the connection
    was returned from _mexe.  After a certain period of time,
    connections are considered stale, and discarded rather than being
    reused.  This saves having to wait for the connection to time out
    if AWS has decided to close it on the other end because of
    inactivity.

    Thread Safety:

        This class is used only fram ConnectionPool while it's mutex
        is held.
    """

    def __init__(self):
        self.queue = []

    def size(self):
        """
        Returns the number of connections in the pool for this host.
        Some of the connections may still be in use, and may not be
        ready to be returned by get().
        """
        return len(self.queue)
    
    def put(self, conn):
        """
        Adds a connection to the pool, along with the time it was
        added.
        """
        self.queue.append((conn, time.time()))

    def get(self):
        """
        Returns the next connection in this pool that is ready to be
        reused.  Returns None of there aren't any.
        """
        # Discard ready connections that are too old.
        self.clean()

        # Return the first connection that is ready, and remove it
        # from the queue.  Connections that aren't ready are returned
        # to the end of the queue with an updated time, on the
        # assumption that somebody is actively reading the response.
        for _ in range(len(self.queue)):
            (conn, _) = self.queue.pop(0)
            if self._conn_ready(conn):
                return conn
            else:
                self.put(conn)
        return None

    def _conn_ready(self, conn):
        """
        There is a nice state diagram at the top of httplib.py.  It
        indicates that once the response headers have been read (which
        _mexe does before adding the connection to the pool), a
        response is attached to the connection, and it stays there
        until it's done reading.  This isn't entirely true: even after
        the client is done reading, the response may be closed, but
        not removed from the connection yet.

        This is ugly, reading a private instance variable, but the
        state we care about isn't available in any public methods.
        """
        if ON_APP_ENGINE:
            # Google App Engine implementation of HTTPConnection doesn't contain
            # _HTTPConnection__response attribute. Moreover, it's not possible
            # to determine if given connection is ready. Reusing connections
            # simply doesn't make sense with App Engine urlfetch service.
            return False
        else:
            response = conn._HTTPConnection__response
            return (response is None) or response.isclosed()

    def clean(self):
        """
        Get rid of stale connections.
        """
        # Note that we do not close the connection here -- somebody
        # may still be reading from it.
        while len(self.queue) > 0 and self._pair_stale(self.queue[0]):
            self.queue.pop(0)

    def _pair_stale(self, pair):
        """
        Returns true of the (connection,time) pair is too old to be
        used.
        """
        (_conn, return_time) = pair
        now = time.time()
        return return_time + ConnectionPool.STALE_DURATION < now

class ConnectionPool(object):

    """
    A connection pool that expires connections after a fixed period of
    time.  This saves time spent waiting for a connection that AWS has
    timed out on the other end.

    This class is thread-safe.
    """

    #
    # The amout of time between calls to clean.
    #
    
    CLEAN_INTERVAL = 5.0

    #
    # How long before a connection becomes "stale" and won't be reused
    # again.  The intention is that this time is less that the timeout
    # period that AWS uses, so we'll never try to reuse a connection
    # and find that AWS is timing it out.
    #
    # Experimentation in July 2011 shows that AWS starts timing things
    # out after three minutes.  The 60 seconds here is conservative so
    # we should never hit that 3-minute timout.
    #

    STALE_DURATION = 60.0

    def __init__(self):
        # Mapping from (host,is_secure) to HostConnectionPool.
        # If a pool becomes empty, it is removed.
        self.host_to_pool = {}
        # The last time the pool was cleaned.
        self.last_clean_time = 0.0
        self.mutex = threading.Lock()

    def size(self):
        """
        Returns the number of connections in the pool.
        """
        return sum(pool.size() for pool in self.host_to_pool.values())

    def get_http_connection(self, host, is_secure):
        """
        Gets a connection from the pool for the named host.  Returns
        None if there is no connection that can be reused.
        """
        self.clean()
        with self.mutex:
            key = (host, is_secure)
            if key not in self.host_to_pool:
                return None
            return self.host_to_pool[key].get()

    def put_http_connection(self, host, is_secure, conn):
        """
        Adds a connection to the pool of connections that can be
        reused for the named host.
        """
        with self.mutex:
            key = (host, is_secure)
            if key not in self.host_to_pool:
                self.host_to_pool[key] = HostConnectionPool()
            self.host_to_pool[key].put(conn)

    def clean(self):
        """
        Clean up the stale connections in all of the pools, and then
        get rid of empty pools.  Pools clean themselves every time a
        connection is fetched; this cleaning takes care of pools that
        aren't being used any more, so nothing is being gotten from
        them. 
        """
        with self.mutex:
            now = time.time()
            if self.last_clean_time + self.CLEAN_INTERVAL < now:
                to_remove = []
                for (host, pool) in self.host_to_pool.items():
                    pool.clean()
                    if pool.size() == 0:
                        to_remove.append(host)
                for host in to_remove:
                    del self.host_to_pool[host]
                self.last_clean_time = now

class HTTPRequest(object):

    def __init__(self, method, protocol, host, port, path, auth_path,
                 params, headers, body):
        """Represents an HTTP request.

        :type method: string
        :param method: The HTTP method name, 'GET', 'POST', 'PUT' etc.

        :type protocol: string
        :param protocol: The http protocol used, 'http' or 'https'.

        :type host: string
        :param host: Host to which the request is addressed. eg. abc.com

        :type port: int
        :param port: port on which the request is being sent. Zero means unset,
                     in which case default port will be chosen.

        :type path: string
        :param path: URL path that is bein accessed.

        :type auth_path: string
        :param path: The part of the URL path used when creating the
                     authentication string.

        :type params: dict
        :param params: HTTP url query parameters, with key as name of the param,
                       and value as value of param.

        :type headers: dict
        :param headers: HTTP headers, with key as name of the header and value
                        as value of header.

        :type body: string
        :param body: Body of the HTTP request. If not present, will be None or
                     empty string ('').
        """
        self.method = method
        self.protocol = protocol
        self.host = host
        self.port = port
        self.path = path
        if auth_path is None:
            auth_path = path
        self.auth_path = auth_path
        self.params = params
        # chunked Transfer-Encoding should act only on PUT request.
        if headers and 'Transfer-Encoding' in headers and \
                headers['Transfer-Encoding'] == 'chunked' and \
                self.method != 'PUT':
            self.headers = headers.copy()
            del self.headers['Transfer-Encoding']
        else:
            self.headers = headers
        self.body = body

    def __str__(self):
        return (('method:(%s) protocol:(%s) host(%s) port(%s) path(%s) '
                 'params(%s) headers(%s) body(%s)') % (self.method,
                 self.protocol, self.host, self.port, self.path, self.params,
                 self.headers, self.body))

    def authorize(self, connection, **kwargs):
        for key in self.headers:
            val = self.headers[key]
            if isinstance(val, unicode):
                self.headers[key] = urllib.quote_plus(val.encode('utf-8'))

        connection._auth_handler.add_auth(self, **kwargs)

        self.headers['User-Agent'] = UserAgent
        # I'm not sure if this is still needed, now that add_auth is
        # setting the content-length for POST requests.
        if not self.headers.has_key('Content-Length'):
            if not self.headers.has_key('Transfer-Encoding') or \
                    self.headers['Transfer-Encoding'] != 'chunked':
                self.headers['Content-Length'] = str(len(self.body))

class AWSAuthConnection(object):
    def __init__(self, host, aws_access_key_id=None, aws_secret_access_key=None,
                 is_secure=True, port=None, proxy=None, proxy_port=None,
                 proxy_user=None, proxy_pass=None, debug=0,
                 https_connection_factory=None, path='/',
                 provider='aws', security_token=None):
        """
        :type host: str
        :param host: The host to make the connection to

        :keyword str aws_access_key_id: Your AWS Access Key ID (provided by
            Amazon). If none is specified, the value in your
            ``AWS_ACCESS_KEY_ID`` environmental variable is used.
        :keyword str aws_secret_access_key: Your AWS Secret Access Key
            (provided by Amazon). If none is specified, the value in your
            ``AWS_SECRET_ACCESS_KEY`` environmental variable is used.

        :type is_secure: boolean
        :param is_secure: Whether the connection is over SSL

        :type https_connection_factory: list or tuple
        :param https_connection_factory: A pair of an HTTP connection
                                         factory and the exceptions to catch.
                                         The factory should have a similar
                                         interface to L{httplib.HTTPSConnection}.

        :param str proxy: Address/hostname for a proxy server

        :type proxy_port: int
        :param proxy_port: The port to use when connecting over a proxy

        :type proxy_user: str
        :param proxy_user: The username to connect with on the proxy

        :type proxy_pass: str
        :param proxy_pass: The password to use when connection over a proxy.

        :type port: int
        :param port: The port to use to connect
        """
        self.num_retries = 5
        # Override passed-in is_secure setting if value was defined in config.
        if config.has_option('Boto', 'is_secure'):
            is_secure = config.getboolean('Boto', 'is_secure')
        self.is_secure = is_secure
        # Whether or not to validate server certificates.  At some point in the
        # future, the default should be flipped to true.
        self.https_validate_certificates = config.getbool(
                'Boto', 'https_validate_certificates', False)
        if self.https_validate_certificates and not HAVE_HTTPS_CONNECTION:
            raise BotoClientError(
                    "SSL server certificate validation is enabled in boto "
                    "configuration, but Python dependencies required to "
                    "support this feature are not available. Certificate "
                    "validation is only supported when running under Python "
                    "2.6 or later.")
        self.ca_certificates_file = config.get_value(
                'Boto', 'ca_certificates_file', DEFAULT_CA_CERTS_FILE)
        self.handle_proxy(proxy, proxy_port, proxy_user, proxy_pass)
        # define exceptions from httplib that we want to catch and retry
        self.http_exceptions = (httplib.HTTPException, socket.error,
                                socket.gaierror)
        # define subclasses of the above that are not retryable.
        self.http_unretryable_exceptions = []
        if HAVE_HTTPS_CONNECTION:
            self.http_unretryable_exceptions.append(ssl.SSLError)
            self.http_unretryable_exceptions.append(
                    https_connection.InvalidCertificateException)

        # define values in socket exceptions we don't want to catch
        self.socket_exception_values = (errno.EINTR,)
        if https_connection_factory is not None:
            self.https_connection_factory = https_connection_factory[0]
            self.http_exceptions += https_connection_factory[1]
        else:
            self.https_connection_factory = None
        if (is_secure):
            self.protocol = 'https'
        else:
            self.protocol = 'http'
        self.host = host
        self.path = path
        if debug:
            self.debug = debug
        else:
            self.debug = config.getint('Boto', 'debug', debug)
        if port:
            self.port = port
        else:
            self.port = PORTS_BY_SECURITY[is_secure]

        # Timeout used to tell httplib how long to wait for socket timeouts.
        # Default is to leave timeout unchanged, which will in turn result in
        # the socket's default global timeout being used. To specify a
        # timeout, set http_socket_timeout in Boto config. Regardless,
        # timeouts will only be applied if Python is 2.6 or greater.
        self.http_connection_kwargs = {}
        if (sys.version_info[0], sys.version_info[1]) >= (2, 6):
            if config.has_option('Boto', 'http_socket_timeout'):
                timeout = config.getint('Boto', 'http_socket_timeout')
                self.http_connection_kwargs['timeout'] = timeout

        self.provider = Provider(provider,
                                 aws_access_key_id,
                                 aws_secret_access_key,
                                 security_token)

        # allow config file to override default host
        if self.provider.host:
            self.host = self.provider.host

        self._pool = ConnectionPool()
        self._connection = (self.server_name(), self.is_secure)
        self._last_rs = None
        self._auth_handler = auth.get_auth_handler(
              host, config, self.provider, self._required_auth_capability())

    def __repr__(self):
        return '%s:%s' % (self.__class__.__name__, self.host)

    def _required_auth_capability(self):
        return []

    def connection(self):
        return self.get_http_connection(*self._connection)
    connection = property(connection)

    def aws_access_key_id(self):
        return self.provider.access_key
    aws_access_key_id = property(aws_access_key_id)
    gs_access_key_id = aws_access_key_id
    access_key = aws_access_key_id

    def aws_secret_access_key(self):
        return self.provider.secret_key
    aws_secret_access_key = property(aws_secret_access_key)
    gs_secret_access_key = aws_secret_access_key
    secret_key = aws_secret_access_key

    def get_path(self, path='/'):
        pos = path.find('?')
        if pos >= 0:
            params = path[pos:]
            path = path[:pos]
        else:
            params = None
        if path[-1] == '/':
            need_trailing = True
        else:
            need_trailing = False
        path_elements = self.path.split('/')
        path_elements.extend(path.split('/'))
        path_elements = [p for p in path_elements if p]
        path = '/' + '/'.join(path_elements)
        if path[-1] != '/' and need_trailing:
            path += '/'
        if params:
            path = path + params
        return path

    def server_name(self, port=None):
        if not port:
            port = self.port
        if port == 80:
            signature_host = self.host
        else:
            # This unfortunate little hack can be attributed to
            # a difference in the 2.6 version of httplib.  In old
            # versions, it would append ":443" to the hostname sent
            # in the Host header and so we needed to make sure we
            # did the same when calculating the V2 signature.  In 2.6
            # (and higher!)
            # it no longer does that.  Hence, this kludge.
            if ((ON_APP_ENGINE and sys.version[:3] == '2.5') or
                    sys.version[:3] in ('2.6', '2.7')) and port == 443:
                signature_host = self.host
            else:
                signature_host = '%s:%d' % (self.host, port)
        return signature_host

    def handle_proxy(self, proxy, proxy_port, proxy_user, proxy_pass):
        self.proxy = proxy
        self.proxy_port = proxy_port
        self.proxy_user = proxy_user
        self.proxy_pass = proxy_pass
        if os.environ.has_key('http_proxy') and not self.proxy:
            pattern = re.compile(
                '(?:http://)?' \
                '(?:(?P<user>\w+):(?P<pass>.*)@)?' \
                '(?P<host>[\w\-\.]+)' \
                '(?::(?P<port>\d+))?'
            )
            match = pattern.match(os.environ['http_proxy'])
            if match:
                self.proxy = match.group('host')
                self.proxy_port = match.group('port')
                self.proxy_user = match.group('user')
                self.proxy_pass = match.group('pass')
        else:
            if not self.proxy:
                self.proxy = config.get_value('Boto', 'proxy', None)
            if not self.proxy_port:
                self.proxy_port = config.get_value('Boto', 'proxy_port', None)
            if not self.proxy_user:
                self.proxy_user = config.get_value('Boto', 'proxy_user', None)
            if not self.proxy_pass:
                self.proxy_pass = config.get_value('Boto', 'proxy_pass', None)

        if not self.proxy_port and self.proxy:
            print "http_proxy environment variable does not specify " \
                "a port, using default"
            self.proxy_port = self.port
        self.use_proxy = (self.proxy != None)

    def get_http_connection(self, host, is_secure):
        conn = self._pool.get_http_connection(host, is_secure)
        if conn is not None:
            return conn
        else:
            return self.new_http_connection(host, is_secure)

    def new_http_connection(self, host, is_secure):
        if self.use_proxy:
            host = '%s:%d' % (self.proxy, int(self.proxy_port))
        if host is None:
            host = self.server_name()
        if is_secure:
            boto.log.debug(
                    'establishing HTTPS connection: host=%s, kwargs=%s',
                    host, self.http_connection_kwargs)
            if self.use_proxy:
                connection = self.proxy_ssl()
            elif self.https_connection_factory:
                connection = self.https_connection_factory(host)
            elif self.https_validate_certificates and HAVE_HTTPS_CONNECTION:
                connection = https_connection.CertValidatingHTTPSConnection(
                        host, ca_certs=self.ca_certificates_file,
                        **self.http_connection_kwargs)
            else:
                connection = httplib.HTTPSConnection(host,
                        **self.http_connection_kwargs)
        else:
            boto.log.debug('establishing HTTP connection: kwargs=%s' %
                    self.http_connection_kwargs)
            connection = httplib.HTTPConnection(host,
                    **self.http_connection_kwargs)
        if self.debug > 1:
            connection.set_debuglevel(self.debug)
        # self.connection must be maintained for backwards-compatibility
        # however, it must be dynamically pulled from the connection pool
        # set a private variable which will enable that
        if host.split(':')[0] == self.host and is_secure == self.is_secure:
            self._connection = (host, is_secure)
        return connection

    def put_http_connection(self, host, is_secure, connection):
        self._pool.put_http_connection(host, is_secure, connection)

    def proxy_ssl(self):
        host = '%s:%d' % (self.host, self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((self.proxy, int(self.proxy_port)))
        except:
            raise
        boto.log.debug("Proxy connection: CONNECT %s HTTP/1.0\r\n", host)
        sock.sendall("CONNECT %s HTTP/1.0\r\n" % host)
        sock.sendall("User-Agent: %s\r\n" % UserAgent)
        if self.proxy_user and self.proxy_pass:
            for k, v in self.get_proxy_auth_header().items():
                sock.sendall("%s: %s\r\n" % (k, v))
        sock.sendall("\r\n")
        resp = httplib.HTTPResponse(sock, strict=True, debuglevel=self.debug)
        resp.begin()

        if resp.status != 200:
            # Fake a socket error, use a code that make it obvious it hasn't
            # been generated by the socket library
            raise socket.error(-71,
                               "Error talking to HTTP proxy %s:%s: %s (%s)" %
                               (self.proxy, self.proxy_port, resp.status, resp.reason))

        # We can safely close the response, it duped the original socket
        resp.close()

        h = httplib.HTTPConnection(host)

        if self.https_validate_certificates and HAVE_HTTPS_CONNECTION:
            boto.log.debug("wrapping ssl socket for proxied connection; "
                           "CA certificate file=%s",
                           self.ca_certificates_file)
            key_file = self.http_connection_kwargs.get('key_file', None)
            cert_file = self.http_connection_kwargs.get('cert_file', None)
            sslSock = ssl.wrap_socket(sock, keyfile=key_file,
                                      certfile=cert_file,
                                      cert_reqs=ssl.CERT_REQUIRED,
                                      ca_certs=self.ca_certificates_file)
            cert = sslSock.getpeercert()
            hostname = self.host.split(':', 0)[0]
            if not https_connection.ValidateCertificateHostname(cert, hostname):
                raise https_connection.InvalidCertificateException(
                        hostname, cert, 'hostname mismatch')
        else:
            # Fallback for old Python without ssl.wrap_socket
            if hasattr(httplib, 'ssl'):
                sslSock = httplib.ssl.SSLSocket(sock)
            else:
                sslSock = socket.ssl(sock, None, None)
                sslSock = httplib.FakeSocket(sock, sslSock)

        # This is a bit unclean
        h.sock = sslSock
        return h

    def prefix_proxy_to_path(self, path, host=None):
        path = self.protocol + '://' + (host or self.server_name()) + path
        return path

    def get_proxy_auth_header(self):
        auth = base64.encodestring(self.proxy_user + ':' + self.proxy_pass)
        return {'Proxy-Authorization': 'Basic %s' % auth}

    def _mexe(self, request, sender=None, override_num_retries=None):
        """
        mexe - Multi-execute inside a loop, retrying multiple times to handle
               transient Internet errors by simply trying again.
               Also handles redirects.

        This code was inspired by the S3Utils classes posted to the boto-users
        Google group by Larry Bates.  Thanks!
        """
        boto.log.debug('Method: %s' % request.method)
        boto.log.debug('Path: %s' % request.path)
        boto.log.debug('Data: %s' % request.body)
        boto.log.debug('Headers: %s' % request.headers)
        boto.log.debug('Host: %s' % request.host)
        response = None
        body = None
        e = None
        if override_num_retries is None:
            num_retries = config.getint('Boto', 'num_retries', self.num_retries)
        else:
            num_retries = override_num_retries
        i = 0
        connection = self.get_http_connection(request.host, self.is_secure)
        while i <= num_retries:
            # Use binary exponential backoff to desynchronize client requests
            next_sleep = random.random() * (2 ** i)
            try:
                # we now re-sign each request before it is retried
                request.authorize(connection=self)
                if callable(sender):
                    response = sender(connection, request.method, request.path,
                                      request.body, request.headers)
                else:
                    connection.request(request.method, request.path, request.body,
                                       request.headers)
                    response = connection.getresponse()
                location = response.getheader('location')
                # -- gross hack --
                # httplib gets confused with chunked responses to HEAD requests
                # so I have to fake it out
                if request.method == 'HEAD' and getattr(response, 'chunked', False):
                    response.chunked = 0
                if response.status == 500 or response.status == 503:
                    boto.log.debug('received %d response, retrying in %3.1f seconds' %
                                   (response.status, next_sleep))
                    body = response.read()
                elif response.status < 300 or response.status >= 400 or \
                        not location:
                    self.put_http_connection(request.host, self.is_secure, connection)
                    return response
                else:
                    scheme, request.host, request.path, params, query, fragment = \
                            urlparse.urlparse(location)
                    if query:
                        request.path += '?' + query
                    boto.log.debug('Redirecting: %s' % scheme + '://' + request.host + request.path)
                    connection = self.get_http_connection(request.host, scheme == 'https')
                    continue
            except self.http_exceptions, e:
                for unretryable in self.http_unretryable_exceptions:
                    if isinstance(e, unretryable):
                        boto.log.debug(
                            'encountered unretryable %s exception, re-raising' %
                            e.__class__.__name__)
                        raise e
                boto.log.debug('encountered %s exception, reconnecting' % \
                                  e.__class__.__name__)
                connection = self.new_http_connection(request.host, self.is_secure)
            time.sleep(next_sleep)
            i += 1
        # If we made it here, it's because we have exhausted our retries and stil haven't
        # succeeded.  So, if we have a response object, use it to raise an exception.
        # Otherwise, raise the exception that must have already happened.
        if response:
            raise BotoServerError(response.status, response.reason, body)
        elif e:
            raise e
        else:
            raise BotoClientError('Please report this exception as a Boto Issue!')

    def build_base_http_request(self, method, path, auth_path,
                                params=None, headers=None, data='', host=None):
        path = self.get_path(path)
        if auth_path is not None:
            auth_path = self.get_path(auth_path)
        if params == None:
            params = {}
        else:
            params = params.copy()
        if headers == None:
            headers = {}
        else:
            headers = headers.copy()
        host = host or self.host
        if self.use_proxy:
            if not auth_path:
                auth_path = path
            path = self.prefix_proxy_to_path(path, host)
            if self.proxy_user and self.proxy_pass and not self.is_secure:
                # If is_secure, we don't have to set the proxy authentication
                # header here, we did that in the CONNECT to the proxy.
                headers.update(self.get_proxy_auth_header())
        return HTTPRequest(method, self.protocol, host, self.port,
                           path, auth_path, params, headers, data)

    def make_request(self, method, path, headers=None, data='', host=None,
                     auth_path=None, sender=None, override_num_retries=None):
        """Makes a request to the server, with stock multiple-retry logic."""
        http_request = self.build_base_http_request(method, path, auth_path,
                                                    {}, headers, data, host)
        return self._mexe(http_request, sender, override_num_retries)

    def close(self):
        """(Optional) Close any open HTTP connections.  This is non-destructive,
        and making a new request will open a connection again."""

        boto.log.debug('closing all HTTP connections')
        self.connection = None  # compat field

class AWSQueryConnection(AWSAuthConnection):

    APIVersion = ''
    ResponseError = BotoServerError

    def __init__(self, aws_access_key_id=None, aws_secret_access_key=None,
                 is_secure=True, port=None, proxy=None, proxy_port=None,
                 proxy_user=None, proxy_pass=None, host=None, debug=0,
                 https_connection_factory=None, path='/', security_token=None):
        AWSAuthConnection.__init__(self, host, aws_access_key_id,
                                   aws_secret_access_key,
                                   is_secure, port, proxy,
                                   proxy_port, proxy_user, proxy_pass,
                                   debug, https_connection_factory, path,
                                   security_token=security_token)

    def _required_auth_capability(self):
        return []

    def get_utf8_value(self, value):
        return boto.utils.get_utf8_value(value)

    def make_request(self, action, params=None, path='/', verb='GET'):
        http_request = self.build_base_http_request(verb, path, None,
                                                    params, {}, '',
                                                    self.server_name())
        if action:
            http_request.params['Action'] = action
        http_request.params['Version'] = self.APIVersion
        return self._mexe(http_request)

    def build_list_params(self, params, items, label):
        if isinstance(items, str):
            items = [items]
        for i in range(1, len(items) + 1):
            params['%s.%d' % (label, i)] = items[i - 1]

    # generics

    def get_list(self, action, params, markers, path='/',
                 parent=None, verb='GET'):
        if not parent:
            parent = self
        response = self.make_request(action, params, path, verb)
        body = response.read()
        boto.log.debug(body)
        if not body:
            boto.log.error('Null body %s' % body)
            raise self.ResponseError(response.status, response.reason, body)
        elif response.status == 200:
            rs = ResultSet(markers)
            h = boto.handler.XmlHandler(rs, parent)
            xml.sax.parseString(body, h)
            return rs
        else:
            boto.log.error('%s %s' % (response.status, response.reason))
            boto.log.error('%s' % body)
            raise self.ResponseError(response.status, response.reason, body)

    def get_object(self, action, params, cls, path='/',
                   parent=None, verb='GET'):
        if not parent:
            parent = self
        response = self.make_request(action, params, path, verb)
        body = response.read()
        boto.log.debug(body)
        if not body:
            boto.log.error('Null body %s' % body)
            raise self.ResponseError(response.status, response.reason, body)
        elif response.status == 200:
            obj = cls(parent)
            h = boto.handler.XmlHandler(obj, parent)
            xml.sax.parseString(body, h)
            return obj
        else:
            boto.log.error('%s %s' % (response.status, response.reason))
            boto.log.error('%s' % body)
            raise self.ResponseError(response.status, response.reason, body)

    def get_status(self, action, params, path='/', parent=None, verb='GET'):
        if not parent:
            parent = self
        response = self.make_request(action, params, path, verb)
        body = response.read()
        boto.log.debug(body)
        if not body:
            boto.log.error('Null body %s' % body)
            raise self.ResponseError(response.status, response.reason, body)
        elif response.status == 200:
            rs = ResultSet()
            h = boto.handler.XmlHandler(rs, parent)
            xml.sax.parseString(body, h)
            return rs.status
        else:
            boto.log.error('%s %s' % (response.status, response.reason))
            boto.log.error('%s' % body)
            raise self.ResponseError(response.status, response.reason, body)
