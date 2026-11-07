"""Read-only Canvas REST client. Authorized token access only."""
import requests

class CanvasClient:
    def __init__(self, base_url, token):
        self.base = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def pages(self, route, **params):
        url = f"{self.base}/api/v1/{route.lstrip('/')}"
        while url:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"Expected collection at {route}")
            yield from payload
            url = response.links.get('next', {}).get('url')
            params = {}

    def get(self, route):
        response = self.session.get(f"{self.base}/api/v1/{route.lstrip('/')}", timeout=30)
        response.raise_for_status()
        return response.json()

    def courses(self):
        return [c for c in self.pages('courses', enrollment_state='active', per_page=100)
                if 'id' in c and not c.get('access_restricted_by_date')]

    def materials(self, course):
        cid = course['id']
        name = course.get('name', str(cid))
        prefix = f'courses/{cid}'
        routes = [
            ('assignment', f'{prefix}/assignments', 'description', 'html_url'),
            ('announcement', f'announcements', 'message', 'html_url'),
            ('page', f'{prefix}/pages', 'body', 'html_url'),
            ('file', f'{prefix}/files', 'display_name', 'url'),
        ]
        for typ, route, field, linkfield in routes:
            try:
                params = {'per_page': 100}
                if typ == 'announcement':
                    params['context_codes[]'] = [f'course_{cid}']
                for obj in self.pages(route, **params):
                    if typ == 'page' and not obj.get('body') and obj.get('url'):
                        obj = self.get(f"{prefix}/pages/{requests.utils.quote(obj['url'], safe='')}")
                    rid = obj.get('id') or obj.get('page_id') or obj.get('url')
                    if rid is None:
                        continue
                    body = obj.get(field) or ''
                    if typ == 'assignment':
                        body += f"\nCanvas due_at: {obj.get('due_at') or 'not specified'}"
                    yield {'key': f'canvas:{cid}:{typ}:{rid}', 'type': typ,
                           'course': name, 'title': obj.get('title') or obj.get('name') or obj.get('display_name') or str(rid),
                           'text': body, 'url': obj.get('html_url') or obj.get(linkfield) or '',
                           'download_url': obj.get('url') if typ == 'file' else None,
                           'filename': obj.get('filename') if typ == 'file' else None,
                           'updated_at': obj.get('updated_at') or obj.get('modified_at') or ''}
            except requests.HTTPError as err:
                if err.response is not None and err.response.status_code in (401, 403, 404):
                    print(f'WARN: unavailable {route} ({err.response.status_code})', flush=True)
                    continue
                raise

    def download(self, url, max_bytes):
        if not url.startswith(('https://', 'http://')):
            raise ValueError('Invalid document URL')
        with requests.get(url, timeout=45, stream=True, allow_redirects=True) as response:
            response.raise_for_status()
            if int(response.headers.get('Content-Length', '0')) > max_bytes:
                raise ValueError('Document too large')
            chunks = []
            total = 0
            for part in response.iter_content(chunk_size=65536):
                total += len(part)
                if total > max_bytes:
                    raise ValueError('Document too large')
                chunks.append(part)
            return b''.join(chunks)
