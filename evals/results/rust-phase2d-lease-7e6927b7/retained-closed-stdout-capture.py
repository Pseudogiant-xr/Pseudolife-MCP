"""Exact retained reset/direct function source; inject the documented capture globals."""

def reset(spec,home):
 if home.exists():list(checked_files(home));shutil.rmtree(home)
 env=fixture_env(home,commands,url)
 for key,value in spec['environment_deltas'].items():
  if value is None:env.pop(key,None)
  else:env[key]=value
 for relative,encoded in spec['pre_files_b64'].items():
  path=file_path(home,relative);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(base64.b64decode(encoded))
 return env

def direct(spec,prefix,home,close_stdout):
 env=reset(spec,home);before=snapshot(home)
 with locked(spec,home) if spec.get('held_file') else contextlib.nullcontext():
  if close_stdout:
   read,write=os.pipe();os.close(read)
   try:child=subprocess.Popen([*prefix,*spec['argv']],cwd=root,env=env,stdin=subprocess.PIPE,stdout=write,stderr=subprocess.PIPE)
   finally:os.close(write)
   stdout,stderr=child.communicate(b'',timeout=10);assert stdout is None
  else:
   child=subprocess.Popen([*prefix,*spec['argv']],cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
   stdout,stderr=child.communicate(b'',timeout=10)
 after=snapshot(home)
 return {'exit_code':child.returncode,'stdout_b64':base64.b64encode(stdout).decode() if stdout is not None else None,'stderr_b64':base64.b64encode(stderr).decode(),'stdout_receiver_closed_before_launch':close_stdout,'effective_argv':[*prefix,*spec['argv']],'environment':env,'cwd':str(root),'pre_files_b64':before,'post_files_b64':after}
