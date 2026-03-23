"""Tests for T2Script lexer, parser, interpreter, and concurrent execution."""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from text2cli.db import WorkspaceDB
from text2cli.lang import execute_script, parse_script, ScriptError, LexError, ParseError
from text2cli.lang.tokens import tokenize, TT
from text2cli.lang.interpreter import StepLimitExceeded


class LexerTest(unittest.TestCase):
    def test_basic_tokens(self):
        tokens = tokenize('let x = 42')
        types = [t.type for t in tokens if t.type != TT.EOF]
        self.assertEqual(types, [TT.LET, TT.IDENT, TT.ASSIGN, TT.NUMBER])

    def test_dotted_command(self):
        tokens = tokenize('fs.read "hello.txt"')
        self.assertEqual(tokens[0].type, TT.DOTTED)
        self.assertEqual(tokens[0].value, "fs.read")
        self.assertEqual(tokens[1].type, TT.STRING)

    def test_pipe(self):
        tokens = tokenize('sort | head 5')
        types = [t.type for t in tokens if t.type != TT.EOF]
        self.assertEqual(types, [TT.IDENT, TT.PIPE, TT.IDENT, TT.NUMBER])

    def test_string_interpolation(self):
        tokens = tokenize('"hello $name"')
        self.assertEqual(tokens[0].type, TT.STRING)
        self.assertIn("${name}", tokens[0].value)

    def test_keywords(self):
        tokens = tokenize('if true { let x = false }')
        types = [t.type for t in tokens if t.type not in (TT.EOF, TT.NEWLINE)]
        self.assertIn(TT.IF, types)
        self.assertIn(TT.TRUE, types)
        self.assertIn(TT.FALSE, types)
        self.assertIn(TT.LET, types)

    def test_comparison_ops(self):
        tokens = tokenize('x == 1 != 2 <= 3 >= 4 < 5 > 6')
        ops = [t.type for t in tokens if t.type in (TT.EQ, TT.NE, TT.LE, TT.GE, TT.LT, TT.GT)]
        self.assertEqual(ops, [TT.EQ, TT.NE, TT.LE, TT.GE, TT.LT, TT.GT])


class ParserTest(unittest.TestCase):
    def test_let_statement(self):
        prog = parse_script('let x = 42')
        self.assertEqual(len(prog.statements), 1)

    def test_pipeline(self):
        prog = parse_script('echo "hello" | sort | head 5')
        self.assertEqual(len(prog.statements), 1)

    def test_if_else(self):
        prog = parse_script('if true { echo "yes" } else { echo "no" }')
        self.assertEqual(len(prog.statements), 1)

    def test_for_loop(self):
        prog = parse_script('for x in [1, 2, 3] { echo $x }')
        self.assertEqual(len(prog.statements), 1)

    def test_fn_def(self):
        prog = parse_script('fn greet(name) { echo "hello" }')
        self.assertEqual(len(prog.statements), 1)

    def test_try_catch(self):
        prog = parse_script('try { echo "ok" } catch e { echo $e }')
        self.assertEqual(len(prog.statements), 1)

    def test_map_literal(self):
        prog = parse_script('let m = {a: 1, b: "two"}')
        self.assertEqual(len(prog.statements), 1)

    def test_list_literal(self):
        prog = parse_script('let items = [1, 2, 3]')
        self.assertEqual(len(prog.statements), 1)

    def test_complex_script(self):
        script = '''
let count = 0
for i in range(5) {
    count = count + 1
}
echo $count
'''
        prog = parse_script(script)
        self.assertGreater(len(prog.statements), 0)


class InterpreterBasicTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "test.db")
        self.db.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, code: str) -> dict:
        return self.db.exec_script("main", code)

    def test_echo(self):
        r = self._run('echo "hello world"')
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["output"], "hello world")

    def test_let_and_interpolation(self):
        r = self._run('let name = "T2"\necho "hello $name"')
        self.assertEqual(r["output"], "hello T2")

    def test_arithmetic(self):
        r = self._run('let x = 2 + 3 * 4\necho $x')
        self.assertEqual(r["output"], "14")

    def test_comparison(self):
        r = self._run('if 5 > 3 { echo "true" } else { echo "false" }')
        self.assertEqual(r["output"], "true")

    def test_string_concat(self):
        r = self._run('let a = "hello"\nlet b = " world"\nlet c = $a + $b\necho $c')
        self.assertEqual(r["output"], "hello world")

    def test_list_operations(self):
        r = self._run('let items = [10, 20, 30]\nlet first = $items[0]\necho $first')
        self.assertEqual(r["output"], "10")

    def test_map_operations(self):
        r = self._run('let m = {name: "alice", age: 30}\nlet n = $m["name"]\necho $n')
        self.assertEqual(r["output"], "alice")

    def test_if_true(self):
        r = self._run('if 1 > 0 { echo "yes" } else { echo "no" }')
        self.assertEqual(r["output"], "yes")

    def test_if_false(self):
        r = self._run('if 0 > 1 { echo "yes" } else { echo "no" }')
        self.assertEqual(r["output"], "no")

    def test_elif(self):
        r = self._run('let x = 5\nif $x > 10 { echo "big" } elif $x > 3 { echo "mid" } else { echo "small" }')
        self.assertEqual(r["output"], "mid")

    def test_for_list(self):
        r = self._run('for x in [1, 2, 3] { echo $x }')
        self.assertEqual(r["output"], "1\n2\n3")

    def test_for_range(self):
        r = self._run('let total = 0\nfor i in range(5) {\n  total = $total + $i\n}\necho $total')
        self.assertEqual(r["output"], "10")

    def test_fn_def_and_call(self):
        r = self._run('fn double(n) { return $n * 2 }\nlet r = double(21)\necho $r')
        self.assertEqual(r["output"], "42")

    def test_try_catch(self):
        r = self._run('try { fs.read "nonexistent.txt" } catch err { echo "caught" }')
        self.assertEqual(r["output"], "caught")

    def test_utility_functions(self):
        r = self._run('let n = len([1, 2, 3])\necho $n')
        self.assertEqual(r["output"], "3")

    def test_step_limit(self):
        with self.assertRaises(Exception):
            self.db.exec_script("main", 'for i in range(99999) { let x = 1 }', max_steps=100)

    def test_nested_fn(self):
        code = '''
fn add(a, b) { return $a + $b }
fn mul(a, b) { return $a * $b }
let r = add(mul(3, 4), 5)
echo $r
'''
        r = self._run(code)
        self.assertEqual(r["output"], "17")


class InterpreterPipeTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "pipe.db")
        self.db.init()
        self.db.write_file("main", "data.txt", "banana\napple\ncherry\napple\nbanana\n")
        self.db.write_file("main", "nums.txt", "5\n3\n8\n1\n4\n")
        self.db.commit_workspace("main", "seed")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, code: str) -> dict:
        return self.db.exec_script("main", code)

    def test_sort_pipe(self):
        r = self._run('fs.read "data.txt" | sort')
        self.assertIn("apple", r["output"])
        lines = r["output"].strip().splitlines()
        self.assertEqual(lines, sorted(lines))

    def test_sort_uniq(self):
        r = self._run('fs.read "data.txt" | sort | uniq')
        lines = r["output"].strip().splitlines()
        self.assertEqual(len(lines), 3)

    def test_head(self):
        r = self._run('fs.read "data.txt" | head 2')
        lines = r["output"].strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_wc_lines(self):
        r = self._run('fs.read "data.txt" | wc -l')
        self.assertEqual(r["output"].strip(), "5")

    def test_grep(self):
        r = self._run('fs.read "data.txt" | grep "apple"')
        lines = r["output"].strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_replace(self):
        r = self._run('fs.read "data.txt" | replace "apple" "APPLE" | grep "APPLE"')
        self.assertIn("APPLE", r["output"])

    def test_numeric_sort(self):
        r = self._run('fs.read "nums.txt" | sort -n')
        lines = r["output"].strip().splitlines()
        self.assertEqual(lines, ["1", "3", "4", "5", "8"])

    def test_pipe_to_write(self):
        r = self._run('fs.read "data.txt" | sort | uniq | fs.write "sorted.txt"')
        self.assertEqual(r["status"], "ok")
        content = self.db.read_file("main", "sorted.txt")["content"]
        self.assertEqual(len(content.strip().splitlines()), 3)

    def test_numeric_sum(self):
        r = self._run('fs.read "nums.txt" | sum')
        self.assertEqual(r["output"].strip(), "21.0")

    def test_upper_lower(self):
        r = self._run('echo "Hello World" | upper')
        self.assertEqual(r["output"], "HELLO WORLD")

    def test_split_join(self):
        r = self._run('echo "a,b,c" | split "," | join "-"')
        self.assertEqual(r["output"], "a-b-c")


class InterpreterFSTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "fs.db")
        self.db.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, code: str) -> dict:
        return self.db.exec_script("main", code)

    def test_write_read(self):
        self._run('fs.write "test.txt" "hello from t2script"')
        r = self._run('let c = fs.read "test.txt"\necho $c')
        self.assertEqual(r["output"], "hello from t2script")

    def test_find_files(self):
        self._run('fs.write "a.py" "pass"\nfs.write "b.py" "pass"\nfs.write "c.md" "# C"')
        r = self._run('let f = fs.find "*.py"\necho $f')
        self.assertIn("a.py", r["output"])
        self.assertIn("b.py", r["output"])
        self.assertNotIn("c.md", r["output"])

    def test_fs_exists(self):
        self._run('fs.write "exists.txt" "yes"')
        r = self._run('let e = fs.exists "exists.txt"\necho $e')
        self.assertIn("true", r["output"])

    def test_commit_and_log(self):
        self._run('fs.write "doc.md" "# Hello"')
        self._run('ws.commit "add doc"')
        r = self._run('let log = ws.log 5\necho $log')
        self.assertIn("add doc", r["output"])

    def test_write_read_delete(self):
        self._run('fs.write "tmp.txt" "temp data"')
        r = self._run('let c = fs.read "tmp.txt"\necho $c')
        self.assertEqual(r["output"], "temp data")
        self._run('fs.delete "tmp.txt"')
        r2 = self._run('try { fs.read "tmp.txt" } catch e { echo "gone" }')
        self.assertEqual(r2["output"], "gone")

    def test_full_workflow(self):
        code = '''
fs.write "src/main.py" "print('hello')"
fs.write "src/utils.py" "def helper(): pass"
fs.write "README.md" "# Project"
ws.commit "init project"
let tree = fs.tree
echo $tree
'''
        r = self._run(code)
        self.assertIn("src", r["output"])
        self.assertIn("README.md", r["output"])


class IntegrationRegressionTest(unittest.TestCase):
    """Regression tests for bugs found in end-to-end testing."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "regr.db")
        self.db.init()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, code: str) -> dict:
        return self.db.exec_script("main", code)

    def test_for_block_not_consumed_as_map(self):
        """{ after command args in for..in must start a block, not a map."""
        r = self._run('for i in range(3) {\n  echo $i\n}')
        self.assertEqual(r["output"], "0\n1\n2")

    def test_for_with_pipeline_in_body(self):
        self._run('fs.write "data.txt" "c\\nb\\na"')
        r = self._run('for f in fs.find "*.txt" {\n  let sorted = fs.read $f | sort\n  echo $sorted\n}')
        self.assertEqual(r["output"].strip(), "a\nb\nc")

    def test_fs_write_no_stdout(self):
        """fs.write should not produce output."""
        r = self._run('fs.write "out.txt" "content"')
        self.assertEqual(r["output"], "")

    def test_map_index_in_command_args(self):
        """$var['key'] in command args should index into the map."""
        r = self._run('let m = {x: "hello"}\necho $m["x"]')
        self.assertEqual(r["output"], "hello")

    def test_import_statement(self):
        self._run('fs.write "lib.t2" "fn add(a, b) {\\n  return \\$a + \\$b\\n}"')
        r = self._run('import "lib.t2"\nlet r = add(3, 4)\necho $r')
        self.assertEqual(r["output"], "7")

    def test_pipeline_result_as_output(self):
        """Standalone pipeline should produce output."""
        self._run('fs.write "nums.txt" "3\\n1\\n2"')
        r = self._run('fs.read "nums.txt" | sort')
        self.assertEqual(r["output"].strip(), "1\n2\n3")

    def test_complex_multiline_script(self):
        r = self._run('''
fn fib(n) {
    if $n <= 1 { return $n }
    let a = fib($n - 1)
    let b = fib($n - 2)
    return $a + $b
}
let result = fib(7)
echo $result
''')
        self.assertEqual(r["output"], "13")


class ConcurrentScriptTest(unittest.TestCase):
    """Multiple agents executing T2Script concurrently in isolated workspaces."""

    AGENT_COUNT = 8

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = WorkspaceDB(Path(self.temp_dir.name) / "concurrent.db")
        self.db.init()
        self.db.write_file("main", "seed.txt", "seed")
        self.db.commit_workspace("main", "seed")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_parallel_scripts(self):
        barrier = threading.Barrier(self.AGENT_COUNT)
        results: list[dict] = [{}] * self.AGENT_COUNT
        errors: list[Exception] = []

        def agent_work(agent_id: int) -> None:
            try:
                ws = f"script-agent-{agent_id}"
                self.db.create_workspace(ws, from_workspace="main")
                barrier.wait(timeout=10)

                code = f'''
fs.write "agent-{agent_id}.txt" "output from agent {agent_id}"
let files = fs.list
ws.commit "agent {agent_id} commit"
echo "agent {agent_id} done"
'''
                result = self.db.exec_script(ws, code)
                results[agent_id] = result
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=agent_work, args=(i,)) for i in range(self.AGENT_COUNT)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [], f"Errors: {errors}")
        for i in range(self.AGENT_COUNT):
            self.assertEqual(results[i]["status"], "ok")
            self.assertIn(f"agent {i} done", results[i]["output"])

    def test_isolation_between_agents(self):
        """Scripts in different workspaces cannot see each other's files."""
        self.db.create_workspace("ws-a", from_workspace="main")
        self.db.create_workspace("ws-b", from_workspace="main")

        self.db.exec_script("ws-a", 'fs.write "only-in-a.txt" "secret"')
        r = self.db.exec_script("ws-b", 'try { fs.read "only-in-a.txt" } catch e { echo "not found" }')
        self.assertEqual(r["output"], "not found")


if __name__ == "__main__":
    unittest.main()
