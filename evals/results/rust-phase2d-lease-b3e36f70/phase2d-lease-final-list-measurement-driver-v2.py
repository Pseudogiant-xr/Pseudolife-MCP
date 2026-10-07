"""Private orchestration of the retained empty-board TOKEN_FILE list case only."""
import argparse,base64,copy,hashlib,importlib.util,json,os,pathlib,subprocess,sys,tempfile,time,traceback,types
P=pathlib.Path(__file__).parent
parser=argparse.ArgumentParser()
parser.add_argument('--host',choices=['windows','linux'],required=True)
parser.add_argument('--candidate-root',type=pathlib.Path,required=True)
parser.add_argument('--oracle-root',type=pathlib.Path,required=True)
parser.add_argument('--candidate-image',type=pathlib.Path,required=True)
parser.add_argument('--image-packet',type=pathlib.Path,required=True)
parser.add_argument('--credential-helper',type=pathlib.Path,required=True)
parser.add_argument('--retired-credential-log',type=pathlib.Path)
parser.add_argument('--expected-head',required=True)
parser.add_argument('--expected-tree',required=True)
clearance_args=parser.add_mutually_exclusive_group(required=True)
clearance_args.add_argument('--board-checked-at')
clearance_args.add_argument('--offline-resource-checked-at')
parser.add_argument('--repeats',type=int,default=3)
parser.add_argument('--samples',type=int,default=10)
parser.add_argument('--smoke',action='store_true')
parser.add_argument('--binding-selfcheck',action='store_true')
parser.add_argument('--out',type=pathlib.Path,required=True)
options=parser.parse_args()
assert not options.out.exists()
if options.repeats<1 or options.samples<1 or (not options.smoke and (options.repeats,options.samples)!=(3,10)):
    parser.error('final measurement requires 3x10; smoke requires positive counts')
host=options.host
assert (host=='windows')==(sys.platform=='win32')
ROOT=options.candidate_root.resolve()
SOURCE=options.oracle_root.resolve(strict=True)
EXE=options.candidate_image.resolve(strict=True)
if host=='linux':
    NATIVE_RUN=pathlib.Path(os.environ['LEASE_NATIVE_EVIDENCE']).resolve(strict=True)
    assert NATIVE_RUN.is_dir()
    assert options.out.parent.resolve()==NATIVE_RUN
    tempfile.tempdir=str(NATIVE_RUN)
else:NATIVE_RUN=options.out.parent.resolve()
os.chdir(SOURCE);sys.path[:0]=[str(SOURCE),str(ROOT)];sys.dont_write_bytecode=True
module=types.ModuleType('evals');module.__path__=[str(ROOT/'evals')];sys.modules['evals']=module
os.environ.update(CUDA_VISIBLE_DEVICES='-1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',PSEUDOLIFE_MCP_NO_SPAWN='1')
import psutil
if host=='windows':psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
else:assert os.getpriority(os.PRIO_PROCESS,0)==10
from evals.rust_port import cli_process
from evals.rust_baseline import cli_measurement,daemon,transport
from evals.rust_port.phase1_ci import postgres
from evals.rust_port.phase1_receipts import candidate_identity,command_identity
from evals.rust_port.provenance import require_import_root,require_instrument_binding,runtime_metadata
from evals.rust_port.stdio_capture import require_phase1_source
from evals.rust_port.full_bank import private_home_overrides
from evals.rust_baseline.daemon import disposable_database,launched_daemon,private_directory
from evals.rust_baseline.common import lease_gate
from pseudolife_memory.storage import embedded_pg
import httpx
credential_spec=importlib.util.spec_from_file_location('private_pg_credential',options.credential_helper.resolve(strict=True))
credential_module=importlib.util.module_from_spec(credential_spec);credential_spec.loader.exec_module(credential_module)
sha=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
def git(*arguments):return subprocess.run(['git',*arguments],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip()
def tick(stage,**values):print(json.dumps(dict(stage=stage,at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),**values)),flush=True)
HEAD=git('rev-parse','HEAD');TREE=git('rev-parse','HEAD^{tree}')
assert HEAD==options.expected_head and TREE==options.expected_tree and not git('status','--porcelain')
require_import_root(SOURCE);pin=require_phase1_source(SOURCE);runtime=runtime_metadata(SOURCE)
assert pin['oracle_head']=='3c01bb31abd60178e15dea99adda369b4bbf92fc' and pin['oracle_schema']==55
assert sys.version_info[:2]==(3,11) and runtime['source_origin_matches_selected_root']
assert runtime['package_runtime_version']==runtime['distribution_versions']['pseudolife-mcp']=='0.17.0'
assert pathlib.Path(sys.prefix).resolve()==(SOURCE.parent/'runtime').resolve()
commands=dict(oracle=[os.path.abspath(sys.executable),'-m','pseudolife_memory.cli'],candidate=[str(EXE)])
identities=dict(oracle=command_identity(commands['oracle'],SOURCE),candidate=candidate_identity(commands['candidate'],ROOT))
header_packet=options.image_packet.resolve(strict=True)
packet=json.loads(header_packet.read_text())
assert packet['candidate_head']==HEAD and packet['candidate_tree']==TREE
assert identities['candidate']['executable_sha256']==packet['platforms'][host]['candidate_sha256']
assert not identities['candidate']['public_cli_module']
case=copy.deepcopy(cli_measurement.lease_list_case())
expected=None
expected_files={'.pseudolife-mcp/token':base64.b64encode(transport.TOKEN.encode('ascii')).decode(),**case['pre_files_b64']}
outer=dict(instrument=require_instrument_binding(ROOT,{'evals/rust_port/phase1_ci.py':[postgres]}),production=require_instrument_binding(SOURCE,{'pseudolife_memory/storage/embedded_pg.py':[pathlib.Path(embedded_pg.__file__),embedded_pg.attach_or_start]}))
helpers={'evals/rust_baseline/daemon.py':[disposable_database,launched_daemon,private_directory],'evals/rust_baseline/daemon_child.py':[pathlib.Path(daemon.__file__).with_name('daemon_child.py')],'evals/rust_baseline/transport.py':[pathlib.Path(transport.__file__)],'evals/rust_port/full_bank.py':[private_home_overrides],'evals/rust_baseline/cli_measurement.py':[pathlib.Path(cli_measurement.__file__)]}
binding=cli_process.cli_binding(ROOT,SOURCE,extra_helpers=helpers)
preserved={}
preserved[str(options.credential_helper.resolve(strict=True))]=sha(options.credential_helper.resolve(strict=True))
result=dict(host=host,head=HEAD,tree=TREE,runtime=runtime,source_pin=pin,command_identities=identities,commands=commands,cli_instrument_binding=binding,outer_owned_PG_binding=outer,inputs=[case],normalizations=[],independently_expected_home_files_b64=expected_files,image_packet_sha256=sha(header_packet),image_build_attestation=packet['platforms'][host],preserved_artifacts_before_sha256=preserved,private_orchestration_sha256=sha(pathlib.Path(__file__)),candidate_cargo_profile='debug',candidate_build_attestation='Immutable final image bound to supplied producing packet; this controller performs no build',prior_first_disposable_proof_reused=True,command=[sys.executable,'-B',str(pathlib.Path(__file__)),*sys.argv[1:]],cwd=str(SOURCE))
if options.binding_selfcheck:
    result.update(status='passed-private-committed-binding-selfcheck',pg_daemon_cli_measurement_launched=False)
    with options.out.open('x',encoding='utf-8') as output:output.write(json.dumps(result,indent=2)+'\n')
    tick('committed-binding-selfcheck',host=host,status=result['status'],PG_daemon_measurement_launched=False)
    raise SystemExit(0)
resource=lease_gate(options.board_checked_at,offline_resource_checked_at=options.offline_resource_checked_at)
result['resource_check']=resource;result['private_runtime_factory_override']={}
before=set(pathlib.Path(tempfile.gettempdir()).glob('plbench_daemon_*'))
def pg_pids():return {p.pid for p in psutil.process_iter(['name']) if (p.info['name'] or '').lower() in ('postgres','postgres.exe')}
previous_pg=pg_pids();owned_pg=[];pg_cleanup=None;cleanup=None;private=None
start=time.monotonic();tick('owned-measurement-start',host=host,head=HEAD,suite_free=resource['local_lock_free'])
try:
    with credential_module.credential_factory(result['private_runtime_factory_override'],options.retired_credential_log) as credentials,postgres() as pg_cleanup:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict,make_conninfo
        params=conninfo_to_dict(os.environ['PSEUDOLIFE_BENCH_ADMIN_URL']);assert params['password']==credentials[0]
        with psycopg.connect(os.environ['PSEUDOLIFE_BENCH_ADMIN_URL'],connect_timeout=5) as auth:assert auth.execute('SELECT 1').fetchone()==(1,)
        result['private_runtime_factory_override']['fresh_credential_authenticates']=True
        rejected=False
        if credentials[1] is not None:
            try:
                with psycopg.connect(make_conninfo(**{**params,'password':credentials[1]}),connect_timeout=5):pass
            except psycopg.OperationalError as error:rejected=error.sqlstate=='28P01' or 'password authentication failed' in str(error)
            assert rejected
        result['private_runtime_factory_override']['retired_credential_authentication_rejected']=rejected if credentials[1] is not None else None
        owned_pg=sorted(pg_pids()-previous_pg);result['owned_postgres_pids']=owned_pg;assert owned_pg
        tick('owned-credential-check',fresh_auth=True,old_auth_rejected=True)
        with disposable_database() as dsn,private_directory() as private:
            try:
                with launched_daemon(dsn,private,source_root=SOURCE,env_extra=private_home_overrides(private),child_module='evals.rust_port.stdio_daemon',startup_timeout=90) as (process,url,cleanup):
                    result.update(owned_url=url,owned_daemon_pid=process.pid)
                    endpoint=url+'/api/coordination/leases'
                    response=httpx.post(endpoint,json={},headers={'Authorization':'Bearer '+transport.TOKEN},timeout=10,trust_env=False,follow_redirects=False)
                    result['independent_endpoint_before']=dict(status_code=response.status_code,body_b64=base64.b64encode(response.content).decode(),body_sha256=hashlib.sha256(response.content).hexdigest())
                    payload=response.json();assert response.status_code==200 and payload.get('enabled') is not False and payload['leases']==[] and payload.get('truncated') is False
                    from types import SimpleNamespace
                    args=SimpleNamespace(oracle_root=SOURCE,candidate_root=ROOT,candidate=EXE,candidate_sha256=identities['candidate']['executable_sha256'],mode='lease-list',argv_json=json.dumps(case['argv']),layout='bare',warm_images=True,repeats=options.repeats,samples=options.samples,smoke=options.smoke,board_checked_at=options.board_checked_at,offline_resource_checked_at=options.offline_resource_checked_at)
                    measured=cli_measurement.measure(args,resource,case=case,fixture_url=url);result['measurement']=measured
                    control_home=pathlib.Path(measured['prepared_controls']['python']['environment']['HOME'])
                    expected_state={'board':{'url':url,'available':True,'reason':None,'truncated':False,'leases':[]},'lock_dir':str(control_home/'.pseudolife-mcp/locks'),'local':[],'test_suite_lock':None}
                    newline='\r\n' if host=='windows' else '\n'
                    expected=(newline.join(json.dumps(expected_state,indent=2).split('\n'))+newline).encode('utf-8')
                    result['independently_expected_stdout_b64']=base64.b64encode(expected).decode()
                    records=dict(id=case['id'],mode=case['mode'])
                    for arm,measured_arm in [('oracle','python'),('candidate','rust')]:
                        raw=measured['byte_control'][measured_arm];cell=measured['prepared_controls'][measured_arm]
                        assert raw['exit_code']==0 and raw['stderr_b64']=='' and base64.b64decode(raw['stdout_b64'])==expected
                        assert cell['pre_files_b64']==cell['post_files_b64']==expected_files
                        env=cell['environment'];assert 'PSEUDOLIFE_MCP_TOKEN' not in env and env['PSEUDOLIFE_MCP_DAEMON_URL']==url
                        assert cell['execution']['selected_prefix']==commands[arm]
                        records[arm]=dict(response={**raw,'post_files_b64':cell['post_files_b64']})
                    result['candidate_output_controls']=cli_process.candidate_controls([records])
                    assert len(result['candidate_output_controls'])==4 and all(c['rejected'] for c in result['candidate_output_controls'])
                    after=httpx.post(endpoint,json={},headers={'Authorization':'Bearer '+transport.TOKEN},timeout=10,trust_env=False,follow_redirects=False)
                    result['independent_endpoint_after']=dict(status_code=after.status_code,body_b64=base64.b64encode(after.content).decode(),body_sha256=hashlib.sha256(after.content).hexdigest())
                    assert after.status_code==200 and after.json()==payload and after.content==response.content
                    result['status']=measured['status'];tick('owned-measurement-captured',host=host,status=result['status'],samples_per_arm=options.repeats*options.samples,controls=4,candidate_profile='debug')
            finally:
                logfile=pathlib.Path(private)/'daemon.log'
                if logfile.exists():
                    data=logfile.read_bytes();(NATIVE_RUN/(options.out.stem+'-daemon.log')).write_bytes(data);result['daemon_log_sha256']=hashlib.sha256(data).hexdigest()
        if cleanup is not None:cleanup['database_dropped']=True
    assert pg_cleanup['owned'] and pg_cleanup['stopped'] and result['private_runtime_factory_override']['original_factory_restored']
    result['private_runtime_factory_override']['owned_new_data_dir_removed']=not pathlib.Path(result['private_runtime_factory_override']['owned_new_data_dir']).exists();assert result['private_runtime_factory_override']['owned_new_data_dir_removed']
    assert all(cleanup[k] for k in ['readiness_identity_verified','daemon_stopped','children_stopped','database_dropped']) and not(set(owned_pg)&pg_pids())
    child=cleanup['actual_child_runtime'];assert child['source_origin_matches_selected_root'] and child['package_runtime_version']==child['distribution_versions']['pseudolife-mcp']=='0.17.0'
    assert cli_process.cli_binding(ROOT,SOURCE,extra_helpers=helpers)==binding
    assert require_instrument_binding(ROOT,{'evals/rust_port/phase1_ci.py':[postgres]})==outer['instrument']
    assert require_instrument_binding(SOURCE,{'pseudolife_memory/storage/embedded_pg.py':[pathlib.Path(embedded_pg.__file__),embedded_pg.attach_or_start]})==outer['production']
    assert require_phase1_source(SOURCE)==pin and candidate_identity(commands['candidate'],ROOT)==identities['candidate'] and command_identity(commands['oracle'],SOURCE)==identities['oracle']
    assert git('rev-parse','HEAD')==HEAD and git('rev-parse','HEAD^{tree}')==TREE and not git('status','--porcelain')
    assert {path:sha(pathlib.Path(path)) for path in preserved}==preserved
    result['preserved_artifacts_after_sha256']={path:sha(pathlib.Path(path)) for path in preserved}
except Exception as error:
    result.update(status='failed-bounded-owned-lease-list',failure_type=type(error).__name__,failure_frames=traceback.format_tb(error.__traceback__),failure_assertion=str(error) if isinstance(error,AssertionError) else 'Exception text withheld to protect possible credential values')
finally:
    result.update(postgres_cleanup=pg_cleanup,daemon_cleanup=cleanup,owned_private_parent_removed=bool(private and not pathlib.Path(private).exists()),owned_postgres_pids_absent=not(set(owned_pg)&pg_pids()),new_private_home_residue=[str(p) for p in set(pathlib.Path(tempfile.gettempdir()).glob('plbench_daemon_*'))-before],elapsed_seconds=time.monotonic()-start,limits=['One retained empty-board TOKEN_FILE lease list fixture, debug native binaries; no release-profile performance or complete lease mode acceptance.','Lease-run has separate functional evidence only; these timings do not establish full lease-core acceptance.'])
    with options.out.open('x',encoding='utf-8') as output:output.write(json.dumps(result,indent=2)+'\n')
tick('owned-measurement-result',host=host,status=result['status'],PG_stopped=bool(pg_cleanup and pg_cleanup['stopped']),private_parent_removed=result['owned_private_parent_removed'],residue_count=len(result['new_private_home_residue']))
raise SystemExit(0 if result['status'] in ('contaminated-plumbing-smoke','quiet-cli-pair-final') else 1)
