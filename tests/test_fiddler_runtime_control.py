"""Exercise the real Fiddler JSON scalar type through JScript.NET, without UI."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess

import pytest


def test_enabled_type_check_accepts_real_fiddler_boolean_only(tmp_path):
    project = Path(__file__).resolve().parents[1]
    compiler = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Microsoft.NET/Framework/v4.0.30319/jsc.exe'
    fiddler = project.parent / 'Fiddler/Fiddler.exe'
    if os.name != 'nt' or not compiler.is_file() or not fiddler.is_file():
        pytest.skip('Local Windows Fiddler/JScript runtime unavailable')
    source = (project / 'capture/fiddler_session.js').read_text(encoding='utf-8-sig')
    expression = re.search(r'!\((control\["enabled"\][^\n]+?)\) \|\|', source).group(1)
    code = '''import System; import System.Collections; import Fiddler;
class GateProbe {
 static function Check(text: String): boolean {
  var control=IDictionary(Fiddler.WebFormats.JSON.JsonDecode(text).JSONObject);
  return EXPRESSION;
 }
}
'''.replace('EXPRESSION', expression)
    src=tmp_path/'GateProbe.js'; dll=tmp_path/'GateProbe.dll'
    src.write_text(code,encoding='utf-8')
    result=subprocess.run([str(compiler),'/nologo','/target:library','/out:'+str(dll),'/reference:'+str(fiddler),str(src)],capture_output=True,timeout=30)
    assert result.returncode==0, result.stdout.decode(errors='replace')
    q=lambda value: "'"+str(value).replace("'","''")+"'"
    script="[void][Reflection.Assembly]::LoadFrom("+q(fiddler)+");$a=[Reflection.Assembly]::LoadFrom("+q(dll)+");$m=$a.GetType('GateProbe').GetMethod('Check');"
    script+="$values=@('true','false','\"true\"','1','null');$out=@();foreach($v in $values){$out+= $m.Invoke($null,@('{\"enabled\":'+$v+'}'))};ConvertTo-Json -Compress -InputObject $out"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',base64.b64encode(script.encode('utf-16le')).decode()],capture_output=True,timeout=30)
    assert result.returncode==0
    assert json.loads(result.stdout.decode('utf-8-sig'))==[True,True,False,False,False]
