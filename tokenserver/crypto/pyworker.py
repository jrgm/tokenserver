import json
import sys
import os
import traceback

from powerhose.client.worker import Worker
from powerhose.util import unserialize
from powerhose.job import Job

from tokenserver import logger
from tokenserver.crypto.master import Response, PROTOBUF_CLASSES

from browserid._m2_monkeypatch import DSA as _DSA
from browserid._m2_monkeypatch import RSA as _RSA
from browserid import jwt

from M2Crypto import BIO


def load_certificates(path, certs=None):
    """load all the certificates stored in the given path.

    In case some certificates are already loaded with the same hostname,
    they will be replaced with the new ones.
    """
    logger.info('loading the certificates located in %s' % path)
    if certs is None:
        certs = {}

    for filename in [os.path.join(path, f) for f in os.listdir(path)
                     if os.path.isfile(os.path.join(path, f))
                     and f.endswith('.crt')]:
        # the files have to be named by the hostname
        algo, hostname = parse_filename(filename)
        cert = get_certificate(algo, filename)
        certs[hostname] = cert
    return certs


def parse_filename(filename):
    """return the algorithm and hostname of the given filename"""
    filename = os.path.basename(filename)
    algo = filename.split('.')[-2]
    hostname = '.'.join(filename.split('.')[:-2])
    return algo, hostname


def get_crypto_obj(algo, filename=None, key=None):
    if filename is None and key is None:
        raise ValueError('you need to specify either filename or key')

    if key is not None:
        bio = BIO.MemoryBuffer(str(key))
    else:
        bio = BIO.openfile(filename)

    # we can know what's the algorithm used thanks to the filename
    if algo.startswith('RS'):
        obj = _RSA.load_pub_key_bio(bio)
    elif algo.startswith('DS'):
        obj = _DSA.load_pub_key_bio(bio)
    else:
        raise ValueError('unknown algorithm')
    return obj


def get_certificate(algo, filename=None, key=None):
    obj = get_crypto_obj(algo, filename, key)
    cls = getattr(jwt, '%sKey' % algo)
    return cls(obj=obj)


class CryptoWorker(object):

    def __init__(self, path):
        logger.info('starting a crypto worker')
        self.serialize = json.dumps
        self.certs = []
        self.certs = load_certificates(path)

    def __call__(self, msg):
        """proxy to the functions exposed by the worker"""
        logger.info('worker called with the message %s' % msg)
        try:
            if isinstance(msg, list):
                data = msg[0]
            else:
                data = msg

            data = unserialize(data)
            job = Job.load_from_string(data[-1])

            try:
                function_id, serialized_data = job.data.split('::', 1)
                obj = PROTOBUF_CLASSES[function_id]()
                obj.ParseFromString(serialized_data)
                data = {}
                for field, value in obj.ListFields():
                    data[field.name] = value

            except ValueError:
                raise ValueError('could not parse data')

            if not hasattr(self, function_id):
                raise ValueError('the function does not exists')

            res = getattr(self, function_id)(**data)
            return Response(value=res).SerializeToString()
        except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            exc = traceback.format_tb(exc_traceback)
            exc.insert(0, str(e))
            return Response(error='\n'.join(exc)).SerializeToString()

    def error(self, message):
        """returns an error message"""
        raise Exception(message)

    def check_signature(self, hostname, signed_data, signature,
                        algorithm=None):
        if algorithm == 'RS':
            return True
        try:
            try:
                return self.certs[hostname].verify(signed_data, signature)
            except KeyError:
                self.error('unknown hostname "%s"' % hostname)
        except:
            self.error('could not check sig for host %r' % hostname)
            raise

    def check_signature_with_cert(self, cert, signed_data, signature,
                                  algorithm):
        try:
            cert = get_certificate(key=cert, algo=algorithm)
            return cert.verify(signed_data, signature)
        except:
            self.error('could not check sig')
            raise

    def derivate_key(self):
        pass


def get_worker(endpoint, path, identity=None):
    pid = str(os.getpid())
    if identity is None:
        identity = 'ipc:///tmp/tokenserver-slave-%s.ipc' % pid
    else:
        identity = identity.replace('$PID', pid)

    return Worker(endpoint, identity, CryptoWorker(os.path.abspath(path)))


if __name__ == '__main__':
    if len(sys.argv) < 1:
        raise ValueError("You should specify the certificates folder")

    worker = get_worker(*sys.argv[1:])
    try:
        worker.run()
    finally:
        worker.stop()
