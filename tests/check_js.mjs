// Parse every <script> body in the app's HTML templates, so a JS syntax error can't ship.
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../app.py', import.meta.url), 'utf8');
let fails = 0, checked = 0;
for (const m of src.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
  const body = m[1];
  if (!body.trim()) continue;
  checked++;
  const line = src.slice(0, m.index).split('\n').length;
  try {
    // Jinja placeholders are server-rendered; substitute a literal so the JS parses.
    new vm.Script(body.replace(/\{\{[^}]*\}\}/g, '""'), { filename: `app.py:${line}` });
    console.log(`  [OK] <script> at app.py:${line} (${body.split('\n').length} lines)`);
  } catch (e) {
    fails++;
    console.log(`  [XX] <script> at app.py:${line}: ${e.message}`);
  }
}
console.log(`\n${checked - fails}/${checked} script blocks parse`);
process.exit(fails ? 1 : 0);
