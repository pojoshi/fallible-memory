// Drives the built demo (demo/index.html) in jsdom: clicks through every scenario and asserts on the DOM.
// Run: npm run build:demo && npm run check:demo
import { JSDOM, VirtualConsole } from 'jsdom';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const html = readFileSync(process.argv[2] ?? join(root, 'demo/index.html'), 'utf8');
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => errors.push(String(e.message || e)));
vc.on('error', (e) => errors.push(String(e)));

const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/', virtualConsole: vc });
const { document, localStorage } = dom.window;
const $ = (id) => document.getElementById(id);
const text = (id) => $(id).textContent;

let failed = 0;
const check = (name, cond, extra = '') => {
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${cond ? '' : '  -> ' + extra}`);
  if (!cond) failed++;
};
const click = (id) => $(id).dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
const choose = (id, value) => { $(id).value = value; $(id).dispatchEvent(new dom.window.Event('change', { bubbles: true })); };
const slot = (scope, subject, predicate) => JSON.stringify([scope, subject, predicate]);
const ask = (ctx, s) => { choose('ctxSelect', ctx); choose('askSelect', s); click('askBtn'); return $('answer'); };
const mode = (a) => a.querySelector('.pill')?.textContent;

// seed state
check('page boots without script errors', errors.length === 0, errors.join(' | '));
let cards = [...document.querySelectorAll('.fact-card')];
check('inspector shows seeded facts (history hidden)', cards.length === 4, `got ${cards.length}`);
check('stale deadline is flagged on day 14', cards.some((c) => c.textContent.includes('Oct 10') && c.className.includes('status-stale')));

// contradiction scenario
click('scContradict');
check('contradiction supersedes and keeps history', text('lastResult').startsWith('Superseded') && text('lastResult').includes('Globex Corp'), text('lastResult'));
$('showHistory').checked = true; $('showHistory').dispatchEvent(new dom.window.Event('change', { bubbles: true }));
check('history view shows retired values struck out', document.querySelectorAll('.fact-value.strike').length >= 2);
$('showHistory').checked = false; $('showHistory').dispatchEvent(new dom.window.Event('change', { bubbles: true }));

// weak vs strong
click('scGuess');
check('inferred guess is disputed, not an overwrite', text('lastResult').startsWith('Disputed'), text('lastResult'));
let a = ask('assistant', slot('user', 'user', 'employer'));
check('agent asks which claim is right', mode(a) === 'ask' && a.textContent.includes('conflicting answers'), a.textContent);
const keepBtn = [...a.querySelectorAll('button')].find((b) => b.textContent.startsWith('Keep'));
keepBtn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
a = ask('assistant', slot('user', 'user', 'employer'));
check('choosing resolves the dispute and the agent asserts', mode(a) === 'assert' && a.textContent.includes('Initech'), a.textContent);

// stale -> ask -> confirm
a = ask('assistant', slot('project:atlas', 'project', 'deadline'));
check('stale inferred fact makes the agent ask', mode(a) === 'ask' && a.textContent.includes('Is that still right?'), a.textContent);
[...a.querySelectorAll('button')].find((b) => b.textContent === 'Yes, still right').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
a = ask('assistant', slot('project:atlas', 'project', 'deadline'));
check('after the user confirms, the agent asserts', mode(a) === 'assert', a.textContent);

// scope + sensitivity
a = ask('bot', slot('user', 'user', 'employer'));
check('project bot cannot see user scope', mode(a) === 'unknown' && !a.textContent.includes('Initech'), a.textContent);
a = ask('assistant', slot('user', 'user', 'home_address'));
check('assistant is denied the sensitive address', mode(a) === 'withheld' && !a.textContent.includes('Palm'), a.textContent);
a = ask('you', slot('user', 'user', 'home_address'));
check('owner asking directly gets the address', mode(a) === 'assert' && a.textContent.includes('12 Palm Road'), a.textContent);

// privacy: sensitive inference rejected
click('scSensitive');
check('inferring a medical condition is rejected', text('lastResult').startsWith('Rejected') && text('lastResult').includes('sensitive'), text('lastResult'));

// log must not retain values once a slot is forgotten
click('scTool');
check('tool correction is logged with its value', JSON.parse(localStorage.getItem('mkw-demo-v1')).log.some((e) => e.text.includes('Nov 3')));
// forget
const addrCard = [...document.querySelectorAll('.fact-card')].find((c) => c.textContent.includes('12 Palm Road'));
addrCard.querySelector('button').dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
check('forgetting reports a tombstone', text('tomb').includes('1 slot(s) forgotten'), text('tomb'));
const visible = (() => { const c = document.body.cloneNode(true); c.querySelectorAll('script').forEach((n) => n.remove()); return c.textContent; })();
check('forgotten value is gone from the visible page', !visible.includes('12 Palm Road'));
check('forgotten value is gone from persisted storage', !(localStorage.getItem('mkw-demo-v1') || '').includes('Palm'));
click('writeBtn'); // empty value: should be a no-op
$('fSubject').value = 'user'; $('fPredicate').value = 'home_address'; $('fValue').value = '99 Elm St'; choose('fSource', 'tool_output'); click('writeBtn');
check('a tombstoned slot cannot be re-inferred', text('lastResult').startsWith('Rejected') && text('lastResult').includes('forget'), text('lastResult'));

// scope forget
choose('filterScope', 'project:atlas');
check('scope-forget button enables when a scope is selected', $('forgetScopeBtn').disabled === false);
dom.window.confirm = () => true;
$('showHistory').checked = false;
click('forgetScopeBtn');
check('forgetting a scope erases values, including from the activity log', !(localStorage.getItem('mkw-demo-v1') || '').includes('Oct 10'), (localStorage.getItem('mkw-demo-v1') || '').slice(0, 400));

// clock
$('dayRange').value = '80'; $('dayRange').dispatchEvent(new dom.window.Event('input', { bubbles: true }));
check('day slider updates the readout', text('dayReadout') === 'Day 80');

// reset
choose('filterScope', 'all');
click('resetBtn');
check('reset restores seed', document.querySelectorAll('.fact-card').length === 4 && text('tomb') === 'Nothing has been forgotten yet.');
check('no script errors during the whole run', errors.length === 0, errors.join(' | '));

console.log(failed ? `\n${failed} check(s) FAILED` : '\nAll demo checks passed');
process.exit(failed ? 1 : 0);
