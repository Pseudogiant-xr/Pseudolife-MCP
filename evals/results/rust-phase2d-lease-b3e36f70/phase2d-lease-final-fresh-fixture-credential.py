"""Fresh password for an owned disposable PostgreSQL fixture."""
from contextlib import contextmanager
import hashlib
import inspect
import pathlib
import re
import secrets

@contextmanager
def credential_factory(proof, retired_credential_log=None):
    import pg0
    original=pg0.Pg0
    retired=None
    if retired_credential_log is not None:
        text=pathlib.Path(retired_credential_log).read_text()
        record=re.search(r'storage: postgres \(([^\r\n]*)\)',text)
        retired=dict(re.findall(r'(\w+)=([^\s]+)',record.group(1)))['password']
    fresh=secrets.token_urlsafe(32)
    assert retired is None or not secrets.compare_digest(fresh,retired)
    source=pathlib.Path(inspect.getsourcefile(original))
    proof.update(original_factory_source_path=str(source),original_factory_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),private_wrapper_source_sha256=hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),credential_only_override=False,fresh_differs_from_retired=None if retired is None else True,original_factory_restored=False,constructor_calls=0)
    def factory(*args,**kwargs):
        assert not args and set(kwargs)=={'name','database','data_dir'}
        directory=pathlib.Path(kwargs['data_dir']).resolve()
        assert directory.name=='embedded_pg' and directory.parent.name.startswith('plbench_daemon_') and directory.is_dir() and not list(directory.iterdir())
        assert proof['constructor_calls']==0
        instance=original(**{**kwargs,'password':fresh})
        proof.update(constructor_calls=1,constructor_args_retained_except_password=True,credential_only_override=True,owned_new_data_dir=str(directory),owned_new_data_dir_verified_empty=True,override_fields=['password'])
        return instance
    pg0.Pg0=factory
    try:
        yield fresh,retired
    finally:
        pg0.Pg0=original
        proof['original_factory_restored']=pg0.Pg0 is original
