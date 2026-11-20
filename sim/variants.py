"""Write sim/bot_<name>.py variants of ../bot.py with constants overridden."""
import os, re, sys
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, '..', 'bot.py')).read()
def make(name, **kw):
    s = src
    for k, v in kw.items():
        s, n = re.subn(rf'^{k} = .*$', f'{k} = {v!r}', s, flags=re.M)
        assert n == 1, k
    open(os.path.join(HERE, f'bot_{name}.py'), 'w').write(s)
if __name__ == '__main__':
    for arg in sys.argv[1:]:
        name, rest = arg.split('=', 1)
        kw = dict(p.split(':') for p in rest.split(';')) if rest else {}
        make(name, **{k: float(v) for k, v in kw.items()})
