"""CloudBase Python HTTP runtime entrypoint (port 9000, stdlib only)."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from .lookup_service import LookupService
except ImportError:
    from lookup_service import LookupService


def create_server(directory=None, port=9000):
    root=Path(directory or Path(__file__).parent)
    service=LookupService(json.loads((root/'data.json').read_text(encoding='utf-8')),
        json.loads((root/'metadata.json').read_text(encoding='utf-8')),
        os.environ.get('HOUSING_LOOKUP_SIGNING_SECRET',''),os.environ.get('HOUSING_LOOKUP_SITE_ORIGIN',''))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass  # No request bodies, headers or bearer URLs in logs.
        def handle_request(self):
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<=length<=2048:raise ValueError()
                body=self.rfile.read(length).decode('utf-8')
            except (ValueError,UnicodeError):
                self.send_response(413);self.send_header('Cache-Control','no-store');self.end_headers();return
            result=service.handle({'path':self.path.split('?',1)[0],'httpMethod':self.command,
                                   'headers':dict(self.headers),'body':body})
            encoded=result['body'].encode('utf-8')
            self.send_response(result['statusCode'])
            # CloudBase's HTTP gateway owns CORS. Duplicating these headers makes
            # browsers reject even identical Allow-Origin values. The service
            # still checks the exact Origin before returning any private data.
            for key,value in result['headers'].items():
                if not key.lower().startswith('access-control-'):self.send_header(key,value)
            self.send_header('Content-Length',str(len(encoded)));self.end_headers()
            if self.command!='HEAD':self.wfile.write(encoded)
        do_GET=do_POST=do_OPTIONS=do_HEAD=do_PUT=do_DELETE=handle_request
    return ThreadingHTTPServer(('0.0.0.0',port),Handler)


if __name__=='__main__':create_server().serve_forever()
