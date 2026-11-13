import os
import subprocess
import sys

cmd = [sys.executable, "raw_scan.py"]
proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))

inputs = [
    "1",       # menu choice: FAST SCAN
    "",        # signature source: default [1] built-in
    "y",       # confirm start
]

# Write all inputs at once and close stdin so the child sees EOF only after all prompts
proc.stdin.write("\n".join(inputs) + "\n")
proc.stdin.close()

output = proc.stdout.read()
print(output)

exit_code = proc.wait(timeout=600)
print(f"Exit code: {exit_code}")
