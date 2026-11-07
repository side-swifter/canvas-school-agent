"""HTTP with retry/backoff for transient failures. Never retries 4xx other than 408/429."""
import logging
import random
import time
import requests

log = logging.getLogger('school-agent.net')
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class RetryExhausted(Exception):
    pass


def request_with_retry(session, method, url, *, attempts=4, base_delay=1.0, sleep=time.sleep, **kwargs):
    kwargs.setdefault('timeout', 30)
    last = None
    for attempt in range(attempts):
        try:
            response = session.request(method, url, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
        else:
            if response.status_code not in RETRY_STATUS:
                return response
            last = RuntimeError(f'HTTP {response.status_code} from {url.split("?")[0]}')
            retry_after = response.headers.get('Retry-After', '')
            if retry_after.isdigit():
                if attempt < attempts - 1:
                    log.warning('Rate limited; sleeping %ss', retry_after)
                    sleep(min(int(retry_after), 60))
                continue
        if attempt < attempts - 1:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.25)
            log.warning('Transient failure (%s); retry %d/%d in %.1fs', last, attempt + 1, attempts - 1, delay)
            sleep(delay)
    raise RetryExhausted(str(last))
