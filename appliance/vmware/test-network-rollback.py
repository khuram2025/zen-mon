import ast,io,os,signal,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock

source=Path(__file__).with_name('console.py').read_text()
node=next(x for x in ast.parse(source).body if isinstance(x,ast.FunctionDef) and x.name=='try_netplan')
namespace={'subprocess':subprocess,'os':os,'signal':signal,'sys':sys,'run':Mock()}
exec(compile(ast.Module(body=[node],type_ignores=[]),'console.py','exec'),namespace)

class RollbackTests(unittest.TestCase):
 def exercise(self,text,code,previous):
  with tempfile.TemporaryDirectory() as folder:
   path=Path(folder)/'management.yaml';path.write_bytes(b'new static settings')
   process=Mock(stdout=io.BytesIO(text));process.wait.return_value=code;process.poll.return_value=code
   namespace['run'].reset_mock()
   with patch.object(subprocess,'Popen',return_value=process),patch.object(sys,'stdout',io.StringIO()):
    namespace['try_netplan'](path,previous)
   return path.read_bytes() if path.exists() else None,namespace['run'].call_count
 def test_zero_exit_timeout_restores_previous_configuration(self):
  self.assertEqual(self.exercise(b'\nReverting.\n',0,b'old DHCP settings'),(b'old DHCP settings',1))
 def test_zero_exit_timeout_removes_new_configuration(self):
  self.assertEqual(self.exercise(b'\nReverting.\n',0,None),(None,1))
 def test_explicit_acceptance_preserves_configuration(self):
  self.assertEqual(self.exercise(b'\nConfiguration accepted.\n',0,b'old settings'),(b'new static settings',0))
 def test_failed_command_never_commits_even_if_acceptance_was_printed(self):
  self.assertEqual(self.exercise(b'\nConfiguration accepted.\n',1,b'old settings'),(b'old settings',1))

if __name__=='__main__':unittest.main()
