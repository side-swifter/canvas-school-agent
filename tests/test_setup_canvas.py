import os, stat
from school_agent.main import setup_canvas, write_env
from tests.helpers import make_cfg, Resp


class S:
    def __init__(self, status): self.status, self.seen = status, []
    def get(self, url, **kw):
        self.seen.append((url, kw)); r = Resp({'name': 'Student'}, self.status); return r


def test_saves_private_env_and_hides_token(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    env = tmp_path / 'secrets' / 'env'
    env.parent.mkdir(); env.write_text('AI_MODEL=keep\nDRY_RUN=false\n')
    s = S(200)
    assert setup_canvas(cfg, 'https://wcpss.instructure.com/', lambda p: 'SECRET123', s) == 0
    out = capsys.readouterr()
    assert 'SECRET123' not in out.out + out.err
    assert s.seen[0][0] == 'https://wcpss.instructure.com/api/v1/users/self'
    text = env.read_text()
    assert 'CANVAS_TOKEN=SECRET123' in text and 'CANVAS_BASE_URL=https://wcpss.instructure.com' in text
    assert 'AI_MODEL=keep' in text and 'DRY_RUN=true' in text and 'DRY_RUN=false' not in text
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600


def test_rejected_token_is_not_saved(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    assert setup_canvas(cfg, 'https://x.instructure.com', lambda p: 'BAD', S(401)) == 1
    assert not (tmp_path / 'secrets' / 'env').exists() and 'BAD' not in capsys.readouterr().out
