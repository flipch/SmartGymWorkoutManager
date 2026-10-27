/**
 * Runs the REAL handlers from templates/create.html, browse.html and index.html under Node with a tiny DOM stub.
 * Run with: node --test tests/templates-ui.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const plain = (x) => JSON.parse(JSON.stringify(x));
const same = (a, b) => assert.deepEqual(plain(a), plain(b));

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const WorkoutLogic = require('../static/workout-logic.js');
const read = (name) => fs.readFileSync(path.join(__dirname, '..', 'templates', name), 'utf8');

/** The source between two markers (both must exist exactly once). */
function between(src, start, end) {
    const a = src.indexOf(start);
    assert.ok(a >= 0, `missing marker: ${start}`);
    const b = src.indexOf(end, a + start.length);
    assert.ok(b > a, `missing end marker: ${end}`);
    return src.slice(a, b);
}

/** A DOM stub: any element, any method; values and classes are remembered per id. */
function makeDocument() {
    const store = {};
    const get = (id) => store[id] ??= {
        id, value: '', textContent: '', innerHTML: '', className: '', min: '',
        classes: new Set(),
        classList: {
            add: (...c) => c.forEach((x) => get(id).classes.add(x)),
            remove: (...c) => c.forEach((x) => get(id).classes.delete(x)),
        },
    };
    return { getElementById: get, store };
}

function metaOf(over = {}) {
    return { isUnilateral: false, isBarbell: false, variants: [], presets: [], dataStatType: null, completionMethod: null, ...over };
}

// ----------------------------------------------------------------------------------------------- the editor
function editor(metaFor) {
    const src = read('create.html');
    const doc = makeDocument();
    const calls = [];
    const ctx = {
        WorkoutLogic, userUnit: 1, workoutData: [], fullLibrary: [], currentTemplateId: null, window: {}, document: doc,
        alert: (m) => calls.push(['alert', m]), renderBuilder() {}, closeImportModal() {}, console,
        fetchExerciseMetadata: async (id) => metaFor(id),
        parseImportedSets: (s, uni, expanded) => WorkoutLogic.parseImportedSets(s, uni, expanded),
        fetch: async (url, opts) => { calls.push(['fetch', url, JSON.parse(opts.body)]); return { json: async () => ({ status: 'error', message: 'x' }) }; },
    };
    vm.createContext(ctx);
    const load = between(src, 'async function loadExistingWorkout', '// --- ADD EXERCISE ---');
    vm.runInContext(load + '\nglobalThis.loadExistingWorkout = loadExistingWorkout;', ctx);
    vm.runInContext(between(src, 'window.processImport = async', '\n    };\n') + '\n};', ctx);
    vm.runInContext(between(src, 'let saveInFlight', '// --- STATS LOGIC ---'), ctx);
    return { ctx, doc, calls };
}

function apiExercise(over = {}) {
    return {
        groupId: 5, actionLibraryId: 55, title: 'T', setsAndReps: '10,10', weights: '20,20', level: '', sportMode: '1,1',
        breakTime2: '30,30', leftRight: '0,0', counterweight2: '', templatePresetId: -1, ...over,
    };
}

test('a Vita template loads its level (not the zero cable weight) into the weight field', async () => {
    const { ctx } = editor(() => metaOf({ dataStatType: 6 }));
    await ctx.loadExistingWorkout({ name: 'n', actionLibraryList: [apiExercise({ weights: '0,0', level: '8,3' })] });
    same(ctx.workoutData[0].sets.map((s) => s.weight), [8, 3]);
});

test('a saved timed set stays timed and a saved rep set stays reps', async () => {
    const { ctx } = editor(() => metaOf());
    await ctx.loadExistingWorkout({ name: 'n', actionLibraryList: [apiExercise({ countType: '2,1', completionMethod: '2,1' })] });
    same(ctx.workoutData[0].sets.map((s) => s.unit), ['sec', 'reps']);
});

test('a saved 0 s rest loads as 0 s', async () => {
    const { ctx } = editor(() => metaOf());
    await ctx.loadExistingWorkout({ name: 'n', actionLibraryList: [apiExercise({ breakTime2: '0,45' })] });
    same(ctx.workoutData[0].sets.map((s) => s.rest), [0, 45]);
});

async function importJson(ctx, doc, data) {
    doc.getElementById('import-json').value = JSON.stringify(data);
    await ctx.window.processImport();
}

test('the AI prompt shape (presetId) and the export shape (preset) both select the preset', async () => {
    const { ctx, doc } = editor(() => metaOf());
    await importJson(ctx, doc, { name: 'n', exercises: [
        { id: 5, presetId: '1', sets: [{ reps: 10, weight: 13, rest: 60 }] },
        { id: 6, preset: 3, sets: [{ reps: 10, weight: 17, rest: 60 }] },
        { id: 7, sets: [{ reps: 10, weight: 20, rest: 60 }] },
        { id: 8, presetId: 'abc', sets: [{ reps: 10, weight: 20, rest: 60 }] },
    ] });
    same(ctx.workoutData.map((e) => e.selectedPresetId), [1, 3, -1, -1]);
});

test('an imported 0 s rest stays 0 s; a missing one defaults to 60', async () => {
    const { ctx, doc } = editor(() => metaOf());
    await importJson(ctx, doc, { name: 'n', exercises: [{ id: 5, sets: [{ reps: 10, weight: 5, rest: 0 }, { reps: 10, weight: 5 }, { reps: 10, weight: 5, rest: '30' }] }] });
    same(ctx.workoutData[0].sets.map((s) => s.rest), [0, 60, 30]);
});

test('a calorie-target exercise is sent as kcal, never as reps', async () => {
    const { ctx, calls } = editor(() => metaOf());
    ctx.workoutData = [{ groupId: 5, selectedVariantId: 55, selectedPresetId: -1, dataStatType: 6, completionMethod: 5, selectCompletionMethod: null,
                         sets: [{ reps: 10, weight: 5, unit: 'reps', rest: 60 }] }];
    await ctx.window.saveWorkout();
    const post = calls.find((c) => c[0] === 'fetch');
    assert.equal(post[2].exercises[0].sets[0].unit, 'kcal');
});

// ----------------------------------------------------------------------------------------------- browse
function browse(fetchImpl, now) {
    const src = read('browse.html');
    const doc = makeDocument();
    const ctx = {
        document: doc, fetch: fetchImpl, console, DIFF_MAP: {}, jsq: (x) => x,
        renderCourseDetail: (d) => { ctx.rendered = d; }, rendered: null,
        Date: now ? class extends Date { constructor(...a) { super(...(a.length ? a : [now])); } } : Date,
    };
    vm.createContext(ctx);
    vm.runInContext('let currentCourseId = null;', ctx);
    const code = between(src, 'function openCourseDetailById', '    function cancelSchedule()')
        + between(src, 'async function confirmSchedule', '    // ── Modal helpers');
    vm.runInContext(code + '\nglobalThis.api = { openCourseDetailById, showScheduleDate, confirmSchedule, current: () => currentCourseId };', ctx);
    return { ctx, doc };
}

test('scheduling targets the course the modal shows, and a slower answer for another course is ignored', async () => {
    const requests = [];
    const waiting = {};
    const fetchImpl = (url, opts) => {
        if (opts) { requests.push(JSON.parse(opts.body)); return Promise.resolve({ json: async () => ({ success: true }) }); }
        const id = Number(url.split('/').pop());
        return new Promise((resolve) => { waiting[id] = () => resolve({ json: async () => ({ id }) }); });
    };
    const { ctx, doc } = browse(fetchImpl);
    ctx.api.openCourseDetailById(101, 'A', 10);
    ctx.api.openCourseDetailById(202, 'B', 20);
    waiting[202]();
    await new Promise((r) => setImmediate(r));
    waiting[101]();  // the slow answer for A arrives last
    await new Promise((r) => setImmediate(r));
    assert.equal(ctx.rendered.id, 202);
    doc.getElementById('scheduleDate').value = '2026-11-01';
    await ctx.api.confirmSchedule();
    assert.equal(requests[0].courseId, 202);
});

test('the earliest schedulable date is the LOCAL date, not the UTC one', () => {
    // 2026-10-27T03:00Z is 20:00 on the 26th in Los Angeles; the test process runs in whatever zone, so pin it
    const { ctx, doc } = browse(() => Promise.resolve({ json: async () => ({}) }), new Date(2026, 9, 26, 20, 0, 0));
    ctx.api.showScheduleDate();
    assert.equal(doc.getElementById('scheduleDate').min, '2026-10-26');
});

// ----------------------------------------------------------------------------------------------- calendar
function calendar(responses) {
    const src = read('index.html');
    const calls = [];
    const alerts = [];
    const ctx = {
        console, alert: (m) => alerts.push(m), fetchCalendarData: () => calls.push('refresh'),
        fetch: async (url, opts) => {
            const body = JSON.parse(opts.body);
            calls.push(`${body.status === 1 ? 'add' : 'remove'}:${body.date}`);
            const next = responses.shift();
            if (next === 'throw') throw new Error('network');
            return { json: async () => next };
        },
        Date,
    };
    vm.createContext(ctx);
    const handler = between(src, 'async function ', 'async function deleteScheduledWorkout');
    // the drop handler is the function whose body contains "data.type === 'move'"
    const start = handler.lastIndexOf('async function ', handler.indexOf("data.type === 'move'"));
    vm.runInContext(handler.slice(start) + '\nglobalThis.drop = ' + /async function (\w+)/.exec(handler.slice(start))[1] + ';', ctx);
    return { ctx, calls, alerts };
}

const move = (ctx) => ctx.drop({ preventDefault() {}, currentTarget: { classList: { remove() {} }, dataset: { date: '2099-01-02' } },
                                   dataTransfer: { getData: () => JSON.stringify({ type: 'move', code: 'abc', originalDate: '2099-01-01' }) } });

test('moving a workout adds the new date first and only then removes the old one', async () => {
    const { ctx, calls } = calendar([{ success: true }, { success: true }]);
    await move(ctx);
    same(calls, ['add:2099-01-02', 'remove:2099-01-01', 'refresh']);
});

test('a failed add leaves the original booking alone', async () => {
    for (const second of [{ success: false, error: 'no' }, 'throw']) {
        const { ctx, calls } = calendar([second]);
        await move(ctx);
        same(calls, ['add:2099-01-02']);
    }
});

test('a failed removal after a good add says so and still refreshes', async () => {
    const { ctx, calls, alerts } = calendar([{ success: true }, { success: false, error: 'busy' }]);
    await move(ctx);
    same(calls, ['add:2099-01-02', 'remove:2099-01-01', 'refresh']);
    assert.match(alerts[0], /now on the new date/);
});

// ----------------------------------------------------------------------------------------------- round 18
test('the AI prompt asks for loads in the ACCOUNT unit', async () => {
    for (const [unit, word, other] of [[1, 'LBS', 'KG'], [0, 'KG', 'LBS']]) {
        const src = read('create.html');
        const prompt = between(src, 'let prompt = `', '`;\n');
        const ctx = { userUnit: unit, userRequest: 'x', out: null };
        vm.createContext(ctx);
        vm.runInContext('out = `' + prompt.slice('let prompt = `'.length) + '`;', ctx);
        assert.ok(ctx.out.includes(`Custom (${word})`), ctx.out.slice(0, 600));
        assert.ok(!ctx.out.includes(`Custom (${other})`));
    }
});

test('an exported timed set stays timed after an import round trip', async () => {
    const { ctx, doc } = editor(() => metaOf());
    const exported = WorkoutLogic.buildExportJSON([{ groupId: 5, title: 'T', selectedPresetId: -1, isUnilateral: false,
        sets: [{ reps: 60, weight: 0, mode: 1, rest: 30, unit: 'sec' }, { reps: 10, weight: 20, mode: 1, rest: 60, unit: 'reps' }] }], 'p');
    await importJson(ctx, doc, exported);
    same(ctx.workoutData[0].sets.map((s) => s.unit), ['sec', 'reps']);
});

function bulkExport(detail) {
    const src = read('index.html');
    let blobText = null;
    const ctx = {
        console, alert() {}, Date, JSON,
        document: { createElement: () => ({ click() {} }), body: { appendChild() {}, removeChild() {} },
                    querySelectorAll: () => [], getElementById: () => ({ classList: { add() {}, remove() {} } }) },
        Blob: class { constructor(parts) { blobText = parts[0]; } },
        URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
    };
    vm.createContext(ctx);
    const body = between(src, 'const exportArr = results.map', 'const jsonStr = JSON.stringify(exportArr');
    vm.runInContext('globalThis.convert = (results) => { ' + body + ' return exportArr; };', ctx);
    return plain(ctx.convert([detail]));
}

test('a bulk export keeps Vita levels, zero rests and timed sets', () => {
    const out = bulkExport({ name: 'n', actionLibraryList: [{ groupId: 5, title: 'T', templatePresetId: -1, setsAndReps: '10,10', weights: '0,0',
        level: '8,3', sportMode: '1,1', breakTime2: '0,45', leftRight: '0,0', counterweight2: '', countType: '2,1' }] });
    same(out[0].exercises[0].sets.map((s) => s.weight), [8, 3]);
    same(out[0].exercises[0].sets.map((s) => s.rest), [0, 45]);
    same(out[0].exercises[0].sets.map((s) => s.unit), ['sec', 'reps']);
});

test('month navigation from the 31st lands in the adjacent month', () => {
    const src = read('index.html');
    const handlers = {};
    const ctx = {
        currentDate: new Date(2026, 9, 31), initCalendar() {},
        document: { getElementById: (id) => ({ addEventListener: (_e, fn) => { handlers[id] = fn; } }) },
    };
    vm.createContext(ctx);
    vm.runInContext(between(src, "document.getElementById('prevMonth')", '    // Initialize'), ctx);
    handlers.nextMonth();
    assert.equal(ctx.currentDate.getMonth(), 10);  // November, not December
    ctx.currentDate = new Date(2026, 9, 31);
    handlers.prevMonth();
    assert.equal(ctx.currentDate.getMonth(), 8);   // September, not October
});

test('a double click on Save sends one create request', async () => {
    const { ctx, calls } = editor(() => metaOf());
    ctx.workoutData = [{ groupId: 5, selectedVariantId: null, selectedPresetId: -1, dataStatType: null, completionMethod: null, selectCompletionMethod: null,
                         sets: [{ reps: 10, weight: 5, unit: 'reps', rest: 60 }] }];
    await Promise.all([ctx.window.saveWorkout(), ctx.window.saveWorkout()]);
    assert.equal(calls.filter((c) => c[0] === 'fetch').length, 1);
    await ctx.window.saveWorkout();  // and a later, deliberate save works again
    assert.equal(calls.filter((c) => c[0] === 'fetch').length, 2);
});

test('an exercise with no known variants sends no variant id (the server uses its own)', async () => {
    const { ctx } = editor(() => metaOf());
    await importJson(ctx, makeDocumentFor(ctx), { name: 'n', exercises: [{ id: 5, sets: [{ reps: 10, weight: 5, rest: 60 }] }] });
    assert.equal(ctx.workoutData[0].selectedVariantId, null);
});
function makeDocumentFor(ctx) { return ctx.document; }

test('only the newest calendar answer draws, and a 401 goes to settings', async () => {
    const src = read('index.html');
    const drawn = [];
    const waiting = [];
    let status = 200;
    const loc = { href: '' };
    const ctx = {
        console, currentDate: new Date(2026, 9, 1), window: { location: loc },
        getMonthString: (d) => `${d.getFullYear()}-${d.getMonth() + 1}`,
        document: { getElementById: () => ({ innerHTML: '' }) },
        renderCalendarGrid: (d) => drawn.push(d.month),
        fetch: (url) => new Promise((resolve) => waiting.push(() => resolve({ status, json: async () => ({ month: url }) }))),
    };
    ctx.window.location = loc;
    vm.createContext(ctx);
    vm.runInContext(between(src, 'let calendarSeq = 0;', '    function renderCalendarGrid') .replace(/window\.location\.href/g, 'loc.href'), Object.assign(ctx, { loc }));
    const a = ctx.fetchCalendarData();
    const b = ctx.fetchCalendarData();
    waiting[1]();  // the newer request answers first
    await b;
    waiting[0]();  // the older one arrives last and must not draw
    await a;
    assert.equal(drawn.length, 1);
    status = 401;
    const c = ctx.fetchCalendarData();
    waiting[2]();
    await c;
    assert.equal(loc.href, '/settings');
});

test('a slower program answer for a program no longer shown is ignored', async () => {
    const src = read('browse.html');
    const waiting = {};
    const doc = makeDocument();
    const rendered = [];
    const ctx = {
        console, document: doc, DIFF_MAP: {}, allPrograms: [{ id: 1 }, { id: 2 }], renderProgramDetail: (d) => rendered.push(d.id),
        fetch: (url) => new Promise((resolve) => { const id = Number(url.split('/').pop()); waiting[id] = () => resolve({ json: async () => ({ id }) }); }),
    };
    vm.createContext(ctx);
    vm.runInContext('let currentProgramId = null;', ctx);
    vm.runInContext(between(src, 'async function openProgramDetail', '    function renderProgramDetail') + '\nglobalThis.open = openProgramDetail;', ctx);
    const a = ctx.open(1);
    const b = ctx.open(2);
    waiting[2](); await b;
    waiting[1](); await a;
    same(rendered, [2]);
});

test('adding an exercise with no known variants sends no variant id', async () => {
    const { ctx } = editor(() => metaOf());
    const src = read('create.html');
    vm.runInContext(between(src, 'async function addExerciseToPlan', '    // --- ').replace('async function addExerciseToPlan', 'globalThis.addExerciseToPlan = async function addExerciseToPlan'), ctx);
    await ctx.addExerciseToPlan(7, 'Press', '');
    assert.equal(ctx.workoutData[0].selectedVariantId, null);
});

// the /coach page lives inside features.py as a Python string
function coachPage(fetchImpl) {
    const py = fs.readFileSync(path.join(__dirname, '..', 'features.py'), 'utf8');
    const html = between(py, '_COACH_HTML = """', '"""\n');
    const script = between(html, '<script>', '</script>').slice('<script>'.length);
    const doc = makeDocument();
    const ctx = { document: doc, console, fetch: fetchImpl, window: {}, JSON, Number, Object, alert() {} };
    vm.createContext(ctx);
    vm.runInContext(script + '\nglobalThis.api = { render, saveDay };', ctx);
    return { ctx, doc };
}

const aProgram = (extra = {}) => ({ review: { score: 80, warnings: [] }, goal_label: 'G', days_per_week: 3, experience: 'x', weekly_sets_per_muscle: {},
    days: [{ name: 'D', warmup: '', setup_swaps: 0, exercises: [] }], disclaimer: 'd', ...extra });

test('the coach page shows unmatched avoid entries and an unreadable feedback file', () => {
    const { ctx, doc } = coachPage(() => Promise.reject(new Error('unused')));
    ctx.api.render(aProgram({ avoid_unmatched: ['lunges <b>'], feedback_error: 'JSONDecodeError' }));
    const html = doc.getElementById('out').innerHTML;
    assert.match(html, /matched no exercise/);
    assert.ok(html.includes('lunges &lt;b&gt;') || !html.includes('<b>'), 'escaped');
    assert.match(html, /could not be fully read \(JSONDecodeError\)/);
    ctx.api.render(aProgram());
    assert.doesNotMatch(doc.getElementById('out').innerHTML, /matched no exercise|could not be fully read/);
});

test('saving a coach day sends one request per click and reports what was not saved and why', async () => {
    let n = 0;
    const answer = { results: [{ created: true, name: 'W', exercise_count: 1, unresolved: [{ name: 'Lunge', reason: 'on your avoid list' }] }],
                     feedback_error: 'QuarantinedCorruptFile' };
    const { ctx, doc } = coachPage(async () => { n += 1; return { status: 200, json: async () => answer }; });
    ctx.api.render(aProgram());
    await Promise.all([ctx.api.saveDay(0), ctx.api.saveDay(0)]);
    assert.equal(n, 1);
    const html = doc.getElementById('save0').innerHTML;
    assert.match(html, /Lunge \(on your avoid list\)/);
    assert.match(html, /QuarantinedCorruptFile/);
});
