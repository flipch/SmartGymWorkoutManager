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
        fetchExerciseMetadata: async (id) => metaFor(id), getSetGoalUnit: (ex) => WorkoutLogic.getSetGoalUnit(ex),
        parseImportedSets: (s, uni, expanded) => WorkoutLogic.parseImportedSets(s, uni, expanded),
        fetch: async (url, opts) => { calls.push(['fetch', url, JSON.parse(opts.body)]); return { json: async () => ({ status: 'error', message: 'x' }) }; },
    };
    vm.createContext(ctx);
    const load = between(src, 'let templateLoading', '// --- ADD EXERCISE ---');
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
        + between(src, 'let scheduleInFlight', '    // ── Modal helpers');
    vm.runInContext(code + '\nglobalThis.api = { openCourseDetailById, showScheduleDate, confirmSchedule, current: () => currentCourseId };', ctx);
    return { ctx, doc };
}

test('scheduling targets the course the modal shows, and a slower answer for another course is ignored', async () => {
    const requests = [];
    const waiting = {};
    const fetchImpl = (url, opts) => {
        if (opts) { requests.push(JSON.parse(opts.body)); return Promise.resolve({ json: async () => ({ success: true }) }); }
        const id = Number(url.split('/').pop());
        return new Promise((resolve) => { waiting[id] = () => resolve({ ok: true, json: async () => ({ id }) }); });
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
        console, alert() {}, Date, JSON, accountUnit: 1,
        document: { createElement: () => ({ click() {} }), body: { appendChild() {}, removeChild() {} },
                    querySelectorAll: () => [], getElementById: () => ({ classList: { add() {}, remove() {} } }) },
        Blob: class { constructor(parts) { blobText = parts[0]; } },
        URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
    };
    vm.createContext(ctx);
    const body = between(src, 'const exportArr = results.map', 'const jsonStr = JSON.stringify(exportArr');
    vm.runInContext('globalThis.convert = (results) => { ' + body + ' return exportArr; };', ctx);
    const out = ctx.convert([detail]);
    return out === undefined ? undefined : plain(out);
}

test('a bulk export keeps Vita levels, zero rests and timed sets', () => {
    const out = bulkExport({ name: 'n', actionLibraryList: [{ groupId: 5, title: 'T', templatePresetId: -1, setsAndReps: '10,10', weights: '0,0',
        level: '8,3', sportMode: '1,1', breakTime2: '0,45', leftRight: '0,0', counterweight2: '', countType: '2,1' }] });
    same(out[0].exercises[0].sets.map((s) => s.weight), [8, 3]);
    same(out[0].exercises[0].sets.map((s) => s.rest), [0, 45]);
    same(out[0].exercises[0].sets.map((s) => s.unit), ['sec', 'reps']);
    assert.equal(out[0].weightUnit, 'lb');  // an export says which unit its weights are in
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
        fetch: (url) => new Promise((resolve) => { const id = Number(url.split('/').pop()); waiting[id] = () => resolve({ ok: true, json: async () => ({ id }) }); }),
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
    vm.runInContext(script + '\nglobalThis.api = { render, saveDay, gen };', ctx);
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

// ----------------------------------------------------------------------------------------------- round 19
function historyPage(fetchImpl) {
    const src = read('history.html');
    const doc = makeDocument();
    const ctx = { document: doc, console, fetch: fetchImpl, TYPE_MAP: {}, Number, Date, JSON };
    vm.createContext(ctx);
    const code = [
        between(src, 'const toMs', '\n') ,
        between(src, 'function fmtDateTime', '    function fmtWeight'),
        between(src, 'let detailSeq', '    function renderDetail'),
    ].join('\n');
    vm.runInContext('let currentDetailPayload = null; let renderedDetail = null; function renderDetail(d, r) { renderedDetail = d; }\n' + code +
        '\nglobalThis.api = { fmtDateTime, openDetail, toMs, rendered: () => renderedDetail };', ctx);
    return { ctx, doc, src };
}

test('history: seconds and milliseconds are the same moment, and sort by real time', () => {
    const { ctx, src } = historyPage(async () => ({}));
    assert.equal(ctx.api.fmtDateTime(1790000000), ctx.api.fmtDateTime(1790000000000));
    const sort = /records\.sort\(([^;]+)\);/.exec(src)[1];
    const compare = vm.runInContext(`(${sort.replace(/\(a, b\) =>/, '(a, b) =>')})`, Object.assign(ctx, {}));
    const records = [{ id: 'older_ms', startTimestamp: 1790000000000 }, { id: 'newer_s', startTimestamp: 1791000000 }];
    records.sort(compare);
    assert.equal(records[0].id, 'newer_s');
});

test('history: a failed detail fetch is an error message, never an empty breakdown', async () => {
    const { ctx, doc } = historyPage(async () => ({ ok: false, status: 502, json: async () => ({ error: 'history_unavailable' }) }));
    await ctx.api.openDetail({ trainingId: 5, type: 5, title: 'x', startTimestamp: 1 });
    assert.equal(ctx.api.rendered(), null);
    assert.match(doc.getElementById('detailLoading').textContent, /history_unavailable/);
});

test('history: a good detail still renders', async () => {
    const { ctx } = historyPage(async () => ({ ok: true, status: 200, json: async () => ({ detail: [] }) }));
    await ctx.api.openDetail({ trainingId: 5, type: 5, title: 'x', startTimestamp: 1 });
    same(ctx.api.rendered(), { detail: [] });
});

test('the editor refuses to open a workout whose exercise details could not be loaded', async () => {
    const { ctx, calls } = editor(() => ({ failed: true, variants: [], presets: [], isUnilateral: false, dataStatType: null }));
    await ctx.loadExistingWorkout({ name: 'n', actionLibraryList: [apiExercise({ weights: '0,0', level: '8,3' })] });
    assert.deepEqual(plain(ctx.workoutData), []);
    assert.ok(calls.some((c) => c[0] === 'alert' && /Could not load/.test(c[1])));
});

test('fetchExerciseMetadata turns an HTTP error body into a failure, not into an empty exercise', async () => {
    const src = read('create.html');
    const ctx = { fetch: async () => ({ ok: false, status: 502, json: async () => ({ error: 'upstream_error' }) }) };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'async function fetchExerciseMetadata', '    async function loadExistingWorkout') + '\nglobalThis.f = fetchExerciseMetadata;', ctx);
    assert.equal((await ctx.f(5)).failed, true);
    ctx.fetch = async () => ({ ok: true, status: 200, json: async () => ({ isLeftRight: 1, dataStatType: 6, actionLibraryList: [{ id: 9 }] }) });
    const good = await ctx.f(5);
    assert.equal(good.failed, undefined);
    assert.equal(good.dataStatType, 6);
});

test('saving carries the unit the numbers were typed in', async () => {
    const { ctx, calls } = editor(() => metaOf());
    ctx.workoutData = [{ groupId: 5, selectedVariantId: null, selectedPresetId: -1, dataStatType: null, completionMethod: null, selectCompletionMethod: null,
                         sets: [{ reps: 10, weight: 100, unit: 'reps', rest: 60 }] }];
    await ctx.window.saveWorkout();
    assert.equal(calls.find((c) => c[0] === 'fetch')[2].unit, 1);  // editor() runs with userUnit = 1
});

test('workout history calendar: the newest month wins and a failed load says so', async () => {
    const src = read('workout_history.html');
    const doc = makeDocument();
    let release;
    const answers = [];
    const ctx = {
        document: doc, console, Date, JSON, Array,
        currentCalendarMonth: 8, currentCalendarYear: 2026, calendarHistoryData: [],
        formatDateInput: (d) => d.toISOString().slice(0, 10),
        fetch: (url) => new Promise((resolve) => answers.push({ url, resolve })),
    };
    vm.createContext(ctx);
    const body = between(src, 'let historyCalendarSeq', '        // Render the calendar grid');
    vm.runInContext(`${body}\n    }\nglobalThis.render = renderHistoryCalendar; globalThis.getData = () => calendarHistoryData;`, ctx);
    const first = ctx.render();
    const second = ctx.render();
    // the NEWER answer lands first, the stale one second
    answers[1].resolve({ ok: true, json: async () => [{ id: 'new' }] });
    await second;
    answers[0].resolve({ ok: true, json: async () => [{ id: 'stale' }] });
    await first;
    assert.deepEqual(plain(ctx.getData()).map((w) => w.id), ['new']);
    // a failed answer is reported, never shown as an empty month
    const third = ctx.render();
    answers[2].resolve({ ok: false, json: async () => ({ error: 'history_unavailable' }) });
    await third;
    assert.match(doc.getElementById('currentMonthLabelHistory').textContent, /could not be loaded/);
    // an HTTP error whose body happens to be a list is still an error
    doc.getElementById('currentMonthLabelHistory').textContent = '';
    const fourth = ctx.render();
    answers[3].resolve({ ok: false, json: async () => [] });
    await fourth;
    assert.match(doc.getElementById('currentMonthLabelHistory').textContent, /could not be loaded/);
});

function historyActions(file, from, to, extra = {}) {
    const src = read(file);
    const doc = makeDocument();
    const calls = [];
    const plainGet = doc.getElementById;
    doc.getElementById = (id) => Object.assign(plainGet(id), { scrollIntoView() {} });
    const ctx = {
        document: Object.assign(doc, { createElement: () => ({ click() {}, style: {} }) }),
        console: { error() {}, log() {} }, JSON, Array, Number, Date, Promise, setTimeout: (f) => f(),
        alert: (m) => calls.push(['alert', m]), confirm: () => true,
        copyToClipboard: (x) => calls.push(['copy', x]),
        Blob: class { constructor(parts) { this.parts = parts; calls.push(['blob', parts.join('')]); } },
        URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
        TYPE_MAP: { 5: { detailType: 'custom' } },
        ...extra,
    };
    vm.createContext(ctx);
    vm.runInContext(between(src, from, to), ctx);
    return { ctx, calls, doc };
}

test('workout history: a failed detail is a message, never a copied or downloaded error object', async () => {
    const { ctx, calls } = historyActions('workout_history.html', 'async function copyDetail', 'async function deepExport', {
        fetch: async () => ({ ok: false, status: 502, json: async () => ({ error: 'history_unavailable' }) }),
    });
    await ctx.copyDetail(5, 'custom');
    await ctx.downloadDetail(5, 'custom');
    assert.equal(calls.filter((c) => c[0] === 'copy' || c[0] === 'blob').length, 0);
    assert.equal(calls.filter((c) => c[0] === 'alert' && /Failed to fetch detail/.test(c[1])).length, 2);
});

test('workout history: the deep export marks a failed record instead of storing the error as its detail', async () => {
    const { ctx, doc } = historyActions('workout_history.html', 'async function deepExport', '    function copyExportArea', {
        currentHistoryData: [{ trainingId: 5, type: 5 }, { trainingId: 6, type: 5 }],
        fetch: async (url) => (url.includes('/6?') ? { ok: true, status: 200, json: async () => ({ code: 0 }) }
                                                    : { ok: false, status: 502, json: async () => ({ error: 'history_unavailable' }) }),
    });
    await ctx.deepExport();
    const rows = JSON.parse(doc.getElementById('export-textarea').value);
    assert.equal(rows[0].detail, undefined);
    assert.match(rows[0].error, /Failed/);
    assert.deepEqual(plain(rows[1].detail), { code: 0 });
});

test('workout history: an export in which every detail failed is not offered as ready', async () => {
    const { ctx, doc, calls } = historyActions('workout_history.html', 'async function deepExport', '    function copyExportArea', {
        currentHistoryData: [{ trainingId: 5, type: 5 }],
        fetch: async () => ({ ok: false, status: 401, json: async () => ({ error: 'Unauthorized' }) }),
    });
    await ctx.deepExport();
    assert.equal(doc.getElementById('export-textarea').value, '');
    assert.ok(calls.some((c) => c[0] === 'alert' && /Export failed/.test(c[1])));
});

test('workout history: a search during the export cannot mix two ranges into one file', async () => {
    const seen = [];
    const { ctx, doc } = historyActions('workout_history.html', 'async function deepExport', '    function copyExportArea', {
        currentHistoryData: [{ trainingId: 1, type: 5 }, { trainingId: 2, type: 5 }],
        fetch: async (url) => { seen.push(url); ctx.currentHistoryData = [{ trainingId: 99, type: 5 }]; return { ok: true, status: 200, json: async () => ({}) }; },
    });
    await ctx.deepExport();
    assert.deepEqual(seen.map((u) => /detail\/(\d+)/.exec(u)[1]), ['1', '2']);
    assert.deepEqual(JSON.parse(doc.getElementById('export-textarea').value).map((r) => r.summary.trainingId), [1, 2]);
});

test('history: the deep export marks a failed record instead of storing the error as its detail', async () => {
    const { ctx, calls } = historyActions('history.html', 'async function deepExportHistory', '    function exportHistory', {
        historyData: { records: [{ trainingId: 5, type: 5 }, { trainingId: 6, type: 5 }], start: 'a', end: 'b' }, loadedRange: { start: '2026-09-01', end: '2026-09-30' },
        fetch: async (url) => (url.includes('/6?') ? { ok: true, status: 200, json: async () => ({ code: 0 }) }
                                                    : { ok: false, status: 502, json: async () => ({ error: 'history_unavailable' }) }),
    });
    await ctx.deepExportHistory();
    const blob = calls.find((c) => c[0] === 'blob');
    const rows = JSON.parse(blob[1]).records;
    assert.deepEqual(plain(JSON.parse(blob[1]).dateRange), { start: '2026-09-01', end: '2026-09-30' });  // the range LOADED, not what the inputs say now
    assert.equal(rows[0].detail, null);
    assert.match(rows[0].error, /history_unavailable/);
});

test('workout history: seconds and milliseconds are the same moment', () => {
    const src = read('workout_history.html');
    const ctx = { Number };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'const toMs', '\n') + '\nglobalThis.toMs = toMs;', ctx);
    assert.equal(ctx.toMs(1790000000), ctx.toMs(1790000000000));
    assert.equal(new Date(ctx.toMs(1790000000)).getFullYear(), 2026);
});

// ----------------------------------------------------------------------------------------------- round 20
const planWith = (n) => Array.from({ length: n }, (_, i) => ({ internalId: i, groupId: 100 + i, title: 'old' + i, sets: [] }));

test('an import written in the other weight unit is refused and changes nothing', async () => {
    for (const [declared, accountUnit] of [['lb', 0], ['kg', 1]]) {
        const { ctx, doc, calls } = editor(() => metaOf());
        ctx.userUnit = accountUnit;
        ctx.workoutData = planWith(2);
        doc.getElementById('plan-name').value = 'keep me';
        await importJson(ctx, doc, { name: 'x', weightUnit: declared, exercises: [{ id: 5, sets: [{ reps: 10, weight: 100, rest: 60 }] }] });
        assert.equal(ctx.workoutData.length, 2, declared);
        assert.equal(doc.getElementById('plan-name').value, 'keep me');
        assert.ok(calls.some((c) => c[0] === 'alert' && /Nothing was imported/.test(c[1])), declared);
    }
    const { ctx, doc } = editor(() => metaOf());
    ctx.userUnit = 1;
    await importJson(ctx, doc, { name: 'x', weightUnit: 'LB', exercises: [{ id: 5, sets: [{ reps: 10, weight: 100, rest: 60 }] }] });
    assert.equal(ctx.workoutData.length, 1);                       // same unit: accepted (case-insensitive)
    await importJson(ctx, doc, { name: 'x', exercises: [{ id: 5, sets: [{ reps: 10, weight: 100, rest: 60 }] }] });
    assert.equal(ctx.workoutData.length, 1);                       // an AI-written file with no unit is still accepted
    await importJson(ctx, doc, { name: 'x', weightUnit: 'stone', exercises: [{ id: 5, sets: [{ reps: 10, weight: 1, rest: 60 }] }] });
    assert.equal(ctx.workoutData[0].sets[0].weight, 100);          // an unknown unit changed nothing
});

test('an import that fails part-way leaves the current plan, its name and its metadata alone', async () => {
    const { ctx, doc, calls } = editor(() => metaOf());
    ctx.workoutData = planWith(3);
    doc.getElementById('plan-name').value = 'keep me';
    await importJson(ctx, doc, { name: 'new', exercises: [{ id: 5, sets: [{ reps: 10, weight: 5, rest: 60 }] }, { id: 6 }] });
    assert.equal(ctx.workoutData.length, 3);
    assert.equal(doc.getElementById('plan-name').value, 'keep me');
    assert.ok(calls.some((c) => c[0] === 'alert' && /Error parsing JSON/.test(c[1])));
    const down = editor((id) => (id === 6 ? { ...metaOf(), failed: true } : metaOf()));
    down.ctx.workoutData = planWith(3);
    await importJson(down.ctx, down.doc, { name: 'new', exercises: [{ id: 5, sets: [{ reps: 10, weight: 5 }] }, { id: 6, sets: [{ reps: 45, weight: 0, unit: 'sec' }] }] });
    assert.equal(down.ctx.workoutData.length, 3);
    assert.ok(down.calls.some((c) => c[0] === 'alert' && /could not be loaded/.test(c[1])));
});

test('adding an exercise while its details cannot be loaded adds nothing', async () => {
    const { ctx, calls } = editor(() => ({ ...metaOf(), failed: true }));
    const src = read('create.html');
    vm.runInContext(between(src, 'async function addExerciseToPlan', '    // --- ').replace('async function addExerciseToPlan', 'globalThis.addExerciseToPlan = async function addExerciseToPlan'), ctx);
    await ctx.addExerciseToPlan(7, 'Press', '');
    assert.equal(ctx.workoutData.length, 0);
    assert.ok(calls.some((c) => c[0] === 'alert' && /not added/.test(c[1])));
});

test('an export names its weight unit', () => {
    const rows = [{ groupId: 1, title: 'T', selectedPresetId: -1, isUnilateral: false, sets: [{ reps: 10, weight: 100, rest: 60 }] }];
    assert.equal(WorkoutLogic.buildExportJSON(rows, 'n', 1).weightUnit, 'lb');
    assert.equal(WorkoutLogic.buildExportJSON(rows, 'n', 0).weightUnit, 'kg');
    assert.equal(JSON.parse(JSON.stringify(WorkoutLogic.buildExportJSON(rows, 'n'))).weightUnit, undefined);
    const src = read('create.html');
    assert.match(src, /WorkoutLogic\.buildExportJSON\(data, name, userUnit\)/);
});

test('history: the newest session opened wins, whatever order the answers arrive in', async () => {
    const waiting = {};
    const { ctx } = historyPage((url) => new Promise((resolve) => { const id = Number(/detail\/(\d+)/.exec(url)[1]); waiting[id] = () => resolve({ ok: true, status: 200, json: async () => ({ detail: [id] }) }); }));
    const a = ctx.api.openDetail({ trainingId: 1, type: 5, title: 'A', startTimestamp: 1 });
    const b = ctx.api.openDetail({ trainingId: 2, type: 5, title: 'B', startTimestamp: 1 });
    waiting[2](); await b;
    waiting[1](); await a;
    same(ctx.api.rendered(), { detail: [2] });
});

test('the coach page says why a program could not be generated', async () => {
    for (const reply of [{ ok: false, status: 400, json: async () => ({ error: 'invalid_readiness', message: 'sleep_hours must be between 0 and 24' }) },
                         { ok: false, status: 500, json: async () => { throw new Error('not json'); } }]) {
        const { ctx, doc } = coachPage(async () => reply);
        await ctx.api.gen();
        assert.doesNotMatch(doc.getElementById('out').innerHTML, /Generating/);
        assert.match(doc.getElementById('out').innerHTML, /sleep_hours|Could not reach/);
    }
});

test('the coach page does not leave Saving… up when the answer is not JSON', async () => {
    const { ctx, doc } = coachPage(async () => ({ ok: false, status: 502, json: async () => { throw new Error('<html>'); } }));
    ctx.api.render(aProgram());
    await ctx.api.saveDay(0);
    assert.match(doc.getElementById('save0').innerHTML, /Unexpected answer \(HTTP 502\)/);
});

test('the insights page does not show an outage as zeros or as "not logged in"', async () => {
    const py = fs.readFileSync(path.join(__dirname, '..', 'features.py'), 'utf8');
    const html = between(py, '_INSIGHTS_HTML = """', '"""\n');
    const script = between(html, '<script>', '</script>').slice('<script>'.length);
    const doc = makeDocument();
    const ctx = { document: doc, console, JSON, Number, Object, Math, Array,
                  fetch: async () => ({ ok: false, status: 503, json: async () => ({ error: 'history_unavailable' }) }) };
    vm.createContext(ctx);
    vm.runInContext(script, ctx);
    await new Promise((r) => setTimeout(r, 20));
    assert.match(doc.getElementById('badge').textContent, /Could not load your insights/);
    assert.doesNotMatch(doc.getElementById('badge').textContent, /Not logged in/);
    assert.doesNotMatch(doc.getElementById('cards').innerHTML, /val>0</);
    assert.match(doc.getElementById('rec').innerHTML, /could not be loaded/);
});

test('browse and exercise pages show a failed load as an error, not as an empty list', async () => {
    const down = async () => ({ ok: false, status: 502, json: async () => ({ error: 'upstream_error' }) });
    const doc = makeDocument();
    const ctx = { document: doc, console, fetch: down, window: { location: {} }, allCourses: [], coursesLoaded: false, programLoaded: false, allPrograms: [] };
    vm.createContext(ctx);
    vm.runInContext(between(read('browse.html'), 'async function fetchCourses', '    function ') + '\nglobalThis.fetchCourses = fetchCourses;', ctx);
    await ctx.fetchCourses();
    assert.equal(ctx.coursesLoaded, false);
    assert.match(doc.getElementById('courseLoading').textContent, /Error/);
    assert.match(read('exercise_detail.html'), /if \(!res\.ok\) throw new Error\(\(json && json\.error\)/);
    const create = read('create.html');
    assert.equal((create.match(/if \(!res\.ok\) throw new Error\('HTTP ' \+ res\.status\);/g) || []).length, 2);
});

test('an import with an unknown weight unit or an empty set list is refused with its reason', async () => {
    const { ctx, doc, calls } = editor(() => metaOf());
    ctx.userUnit = 0;
    ctx.workoutData = planWith(1);
    await importJson(ctx, doc, { name: 'x', weightUnit: 'stone', exercises: [{ id: 5, sets: [{ reps: 10, weight: 1 }] }] });
    assert.ok(calls.some((c) => c[0] === 'alert' && /must be 'kg' or 'lb'/.test(c[1])));
    await importJson(ctx, doc, { name: 'x', exercises: [{ id: 5, sets: [] }] });
    assert.ok(calls.some((c) => c[0] === 'alert' && /no "sets" list/.test(c[1])));
    assert.equal(ctx.workoutData.length, 1);
    assert.equal(ctx.workoutData[0].title, 'old0');
});

test('history: a slow FAILED answer for an older session does not replace the newer session\'s detail', async () => {
    const waiting = {};
    const { ctx, doc } = historyPage((url) => new Promise((resolve) => { const id = Number(/detail\/(\d+)/.exec(url)[1]); waiting[id] = resolve; }));
    const a = ctx.api.openDetail({ trainingId: 1, type: 5, title: 'A', startTimestamp: 1 });
    const b = ctx.api.openDetail({ trainingId: 2, type: 5, title: 'B', startTimestamp: 1 });
    waiting[2]({ ok: true, status: 200, json: async () => ({ detail: [2] }) }); await b;
    waiting[1]({ ok: false, status: 502, json: async () => ({ error: 'history_unavailable' }) }); await a;
    assert.doesNotMatch(doc.getElementById('detailLoading').textContent, /history_unavailable/);
    same(ctx.api.rendered(), { detail: [2] });
});

test('history: a slow network failure for an older session does not replace the newer session\'s detail', async () => {
    const waiting = {};
    const { ctx, doc } = historyPage((url) => new Promise((resolve, reject) => { const id = Number(/detail\/(\d+)/.exec(url)[1]); waiting[id] = { resolve, reject }; }));
    const a = ctx.api.openDetail({ trainingId: 1, type: 5, title: 'A', startTimestamp: 1 });
    const b = ctx.api.openDetail({ trainingId: 2, type: 5, title: 'B', startTimestamp: 1 });
    waiting[2].resolve({ ok: true, status: 200, json: async () => ({ detail: [2] }) }); await b;
    waiting[1].reject(new Error('network down')); await a;
    assert.doesNotMatch(doc.getElementById('detailLoading').textContent, /network down/);
});

test('an imported barbell exercise keeps its 7 kg minimum', async () => {
    const { ctx, doc } = editor(() => metaOf({ isBarbell: true }));
    await importJson(ctx, doc, { name: 'x', exercises: [{ id: 5, sets: [{ reps: 10, weight: 20, rest: 60 }] }] });
    assert.equal(ctx.workoutData[0].isBarbell, true);
});

test('a failed course or program detail is an error, never an empty record', async () => {
    const doc = makeDocument();
    const down = async () => ({ ok: false, status: 502, json: async () => ({ error: 'upstream_error' }) });
    const rendered = [];
    const ctx = { document: doc, console, fetch: down, renderCourseDetail: (d) => rendered.push(d), renderProgramDetail: (d) => rendered.push(d),
                  window: {}, resetScheduleUI() {}, DIFF_MAP: {}, allPrograms: [], allCourses: [] };
    vm.createContext(ctx);
    vm.runInContext('let currentCourseId = null, currentProgramId = null;', ctx);
    const src = read('browse.html');
    vm.runInContext(between(src, 'async function openCourseDetail', '    function renderCourseDetail') + '\nglobalThis.openCourse = openCourseDetail;', ctx);
    vm.runInContext(between(src, 'async function openProgramDetail', '    function renderProgramDetail') + '\nglobalThis.openProgram = openProgramDetail;', ctx);
    await ctx.openCourse(1);
    await ctx.openProgram(2);
    assert.equal(rendered.length, 0);
    assert.match(doc.getElementById('courseModalLoading').textContent, /Error/);
    assert.match(doc.getElementById('programModalLoading').textContent, /Error/);
});

test('a failed last-max-weight lookup shows as unavailable, not as no history', () => {
    const src = read('create.html');
    const ctx = { shouldLoadLastMaxWeight: () => true, lastMaxWeightElementId: () => 'chip', formatLastMaxWeight: (x) => String(x) };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'function buildLastMaxWeightHtml', '    function applyStatsSummaryToExercise') + '\nglobalThis.build = buildLastMaxWeightHtml;', ctx);
    assert.match(ctx.build({ lastMaxWeightStatus: 'error' }), /unavailable/);
    assert.equal(ctx.build({ lastMaxWeightStatus: 'none' }), '');
    assert.match(ctx.build({ lastMaxWeightStatus: 'loaded', lastMaxWeight: 50 }), /Last Max Wt.: 50/);
});

test('the workout history calendar puts a session on the day it STARTED', async () => {
    const src = read('workout_history.html');
    assert.match(src, /new Date\(toMs\(workout\.startTimestamp \|\| workout\.endTimestamp\)\);  \/\/ the START day/);
});

test('the coach page says when the stored readiness could not be read', () => {
    const { ctx, doc } = coachPage(() => Promise.reject(new Error('unused')));
    ctx.api.render(aProgram({ readiness_error: 'JSONDecodeError' }));
    assert.match(doc.getElementById('out').innerHTML, /stored readiness could not be read \(JSONDecodeError\)/);
    ctx.api.render(aProgram());
    assert.doesNotMatch(doc.getElementById('out').innerHTML, /stored readiness could not be read/);
});

// ----------------------------------------------------------------------------------------------- round 22
test('saving or adding while the existing template is still loading is refused', async () => {
    let release;
    const slow = new Promise((r) => { release = r; });
    const { ctx, calls } = editor((id) => slow.then(() => metaOf()));
    const src = read('create.html');
    vm.runInContext(between(src, 'async function addExerciseToPlan', '    // --- ').replace('async function addExerciseToPlan', 'globalThis.addExerciseToPlan = async function addExerciseToPlan'), ctx);
    const loading = ctx.loadExistingWorkout({ name: 'n', actionLibraryList: [apiExercise()] });
    await ctx.window.saveWorkout();
    await ctx.addExerciseToPlan(7, 'Press', '');
    assert.equal(calls.filter((c) => c[0] === 'fetch').length, 0);
    assert.equal(calls.filter((c) => c[0] === 'alert' && /still loading/.test(c[1])).length, 2);
    release();
    await loading;
    assert.equal(ctx.workoutData.length, 1);
    await ctx.addExerciseToPlan(7, 'Press', '');
    assert.equal(ctx.workoutData.length, 2);  // loaded: adding works again
});

test('a failed course detail opened from a program day shows an error', async () => {
    const { ctx, doc } = browse(() => Promise.resolve({ ok: false, status: 502, json: async () => ({ error: 'upstream_error' }) }));
    ctx.api.openCourseDetailById(101, 'A', 10);
    await new Promise((r) => setImmediate(r));
    assert.equal(ctx.rendered, null);
    assert.match(doc.getElementById('courseModalLoading').textContent, /Error: upstream_error/);
});

test('an older calendar failure does not replace the newer calendar', async () => {
    const src = read('index.html');
    const doc = makeDocument();
    const answers = [];
    const drawn = [];
    const ctx = { document: doc, console, window: { location: {} }, currentDate: new Date(2026, 9, 26), getMonthString: () => '2026-10',
                  renderCalendarGrid: (d) => drawn.push(d), fetch: () => new Promise((resolve, reject) => answers.push({ resolve, reject })) };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'let calendarSeq', '    function renderCalendarGrid') + '\nglobalThis.fetchCalendarData = fetchCalendarData;', ctx);
    const first = ctx.fetchCalendarData();
    const second = ctx.fetchCalendarData();
    answers[1].resolve({ status: 200, json: async () => [{ date: '2026-10-01' }] }); await second;
    doc.getElementById('calendarGrid').innerHTML = 'NEWER';
    answers[0].reject(new Error('network')); await first;
    assert.equal(doc.getElementById('calendarGrid').innerHTML, 'NEWER');
    assert.equal(drawn.length, 1);
});

// ----------------------------------------------------------------------------------------------- round 23
const aSet = (over = {}) => ({ reps: 10, weight: 40, rest: 60, ...over });

test('an editor whose template failed to load refuses to save, add or import', async () => {
    const { ctx, doc, calls } = editor(() => ({ ...metaOf(), failed: true }));
    const src = read('create.html');
    vm.runInContext(between(src, 'async function addExerciseToPlan', '    // --- ').replace('async function addExerciseToPlan', 'globalThis.addExerciseToPlan = async function addExerciseToPlan'), ctx);
    await ctx.loadExistingWorkout({ name: 'n', actionLibraryList: [apiExercise()] });
    assert.equal(ctx.workoutData.length, 0);
    await ctx.window.saveWorkout();
    await ctx.addExerciseToPlan(7, 'Press', '');
    await importJson(ctx, doc, { name: 'x', exercises: [{ id: 5, sets: [aSet()] }] });
    assert.equal(calls.filter((c) => c[0] === 'fetch').length, 0);
    assert.equal(ctx.workoutData.length, 0);
    assert.equal(calls.filter((c) => c[0] === 'alert' && /could not be loaded, so editing is disabled/.test(c[1])).length, 3);
});

test('an import cannot run while the existing template is still loading', async () => {
    let release;
    const slow = new Promise((r) => { release = r; });
    const { ctx, doc, calls } = editor(() => slow.then(() => metaOf()));
    const loading = ctx.loadExistingWorkout({ name: 'n', actionLibraryList: [apiExercise()] });
    await importJson(ctx, doc, { name: 'x', exercises: [{ id: 5, sets: [aSet()] }] });
    assert.ok(calls.some((c) => c[0] === 'alert' && /still loading/.test(c[1])));
    release(); await loading;
    assert.equal(ctx.workoutData.length, 1);
    assert.equal(ctx.workoutData[0].title, 'T');  // the loaded template, not the import
});

test('an import refuses missing weights, unknown presets and empty plans, and holds weights to the machine', async () => {
    const run = async (exercises, meta = metaOf()) => {
        const e = editor(() => meta);
        e.ctx.userUnit = 0;
        e.ctx.workoutData = planWith(1);
        await importJson(e.ctx, e.doc, { name: 'x', exercises });
        return e;
    };
    for (const weight of ['bodyweight', '', null, undefined, -5, 0]) {
        const e = await run([{ id: 5, sets: [aSet({ weight })] }]);
        assert.equal(e.ctx.workoutData[0].title, 'old0', String(weight));
        assert.ok(e.calls.some((c) => c[0] === 'alert' && /needs a weight above 0/.test(c[1])), String(weight));
    }
    let e = await run([{ id: 5, presetId: 99, sets: [aSet()] }]);
    assert.ok(e.calls.some((c) => c[0] === 'alert' && /preset 99 is not available/.test(c[1])));
    e = await run([]);
    assert.ok(e.calls.some((c) => c[0] === 'alert' && /no exercises/.test(c[1])));
    assert.equal(e.ctx.workoutData[0].title, 'old0');
    e = await run([{ id: 5, sets: [aSet({ weight: 2 }), aSet({ weight: 500 }), aSet({ weight: 40 })] }]);
    same(e.ctx.workoutData[0].sets.map((s) => s.weight), [3.5, 100, 40]);
    assert.ok(e.calls.some((c) => c[0] === 'alert' && /2 weight value\(s\)/.test(c[1])));
    e = await run([{ id: 5, sets: [aSet({ weight: 0, unit: 'sec', reps: 60 })] }]);
    assert.equal(e.ctx.workoutData[0].sets[0].weight, 0);  // a timed set may carry no load
    e = await run([{ id: 5, sets: [aSet({ weight: 50 })] }], metaOf({ dataStatType: 6 }));
    assert.equal(e.ctx.workoutData[0].sets[0].weight, 10);  // a Vita level tops out at 10
    e = await run([{ id: 5, presetId: 1, sets: [aSet({ weight: 11 })] }], metaOf({ presets: [{ id: 1 }, { id: 3 }] }));
    assert.equal(e.ctx.workoutData[0].selectedPresetId, 1);
});

test('adding a Vita exercise starts at a mid level, whatever the recommended cable weight', async () => {
    const { ctx } = editor(() => metaOf({ dataStatType: 6, recommendedWeight: 25 }));
    const src = read('create.html');
    vm.runInContext(between(src, 'async function addExerciseToPlan', '    // --- ').replace('async function addExerciseToPlan', 'globalThis.addExerciseToPlan = async function addExerciseToPlan'), ctx);
    await ctx.addExerciseToPlan(7, 'Vita', '');
    assert.equal(ctx.workoutData[0].sets[0].weight, 5);
});

test('changing the preset keeps a timed hold and a Vita level', () => {
    const src = read('create.html');
    const ctx = { workoutData: [], getRules: (ex) => WorkoutLogic.getRules(ex, 0), renderBuilder() {}, window: {} };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'window.updateEx = ', '    window.updateSet') + '\n', ctx);
    ctx.workoutData = [{ selectedPresetId: -1, dataStatType: 6, sets: [{ reps: 8, weight: 3, rest: 30, unit: 'reps' }] },
                       { selectedPresetId: -1, dataStatType: null, sets: [{ reps: 60, weight: 20, rest: 30, unit: 'sec' }, { reps: 8, weight: 20, rest: 30, unit: 'reps' }] }];
    ctx.window.updateEx(0, 'selectedPresetId', '1');
    ctx.window.updateEx(1, 'selectedPresetId', '1');
    assert.equal(ctx.workoutData[0].sets[0].weight, 3);
    assert.equal(ctx.workoutData[1].sets[0].reps, 60);
    assert.notEqual(ctx.workoutData[1].sets[1].reps, 8);
    assert.notEqual(ctx.workoutData[1].sets[0].weight, 20);
});

test('a save that gets no valid answer says so', async () => {
    const { ctx, calls } = editor(() => metaOf());
    ctx.workoutData = [{ groupId: 5, selectedVariantId: null, selectedPresetId: -1, dataStatType: null, completionMethod: null, selectCompletionMethod: null,
                         sets: [{ reps: 10, weight: 40, unit: 'reps', rest: 60 }] }];
    ctx.fetch = async () => { throw new Error('offline'); };
    await ctx.window.saveWorkout();
    assert.ok(calls.some((c) => c[0] === 'alert' && /Could not save/.test(c[1])));
});

test('history: only the newest range loads, and an export is named for the range that was loaded', async () => {
    const src = read('history.html');
    const doc = makeDocument();
    const waiting = [];
    const ctx = { document: doc, console, alert() {}, window: { location: {} }, JSON, historyData: 'old',
                  fetch: () => new Promise((resolve) => waiting.push(resolve)), renderHistory: (d) => { ctx.drawn = d; } };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'let historySeq', '    function renderHistory') + '\nglobalThis.fetchHistory = fetchHistory; globalThis.range = () => loadedRange;', ctx);
    doc.getElementById('startDate').value = '2026-09-01'; doc.getElementById('endDate').value = '2026-09-30';
    const a = ctx.fetchHistory();
    doc.getElementById('startDate').value = '2026-10-01'; doc.getElementById('endDate').value = '2026-10-31';
    const b = ctx.fetchHistory();
    assert.equal(ctx.historyData, null);  // nothing stale stays exportable while a load is in flight
    waiting[1]({ ok: true, status: 200, json: async () => ({ records: ['oct'] }) }); await b;
    waiting[0]({ ok: true, status: 200, json: async () => ({ records: ['sep'] }) }); await a;
    same(ctx.historyData, { records: ['oct'] });
    same(ctx.range(), { start: '2026-10-01', end: '2026-10-31' });
});

test('history detail shows the time that was held, not the time that was prescribed', () => {
    assert.match(read('history.html'), /const dur = \(s\.time \?\? s\.targetCount\) \|\| 0;/);
});

test('a double click on Schedule books once', async () => {
    const posts = [];
    let release;
    const gate = new Promise((r) => { release = r; });
    const { ctx, doc } = browse((url, opts) => { if (opts) posts.push(opts.body); return gate.then(() => ({ ok: true, status: 200, json: async () => ({ success: true }) })); });
    ctx.api.openCourseDetailById(101, 'A', 10);
    doc.getElementById('scheduleDate').value = '2026-11-01';
    const one = ctx.api.confirmSchedule();
    const two = ctx.api.confirmSchedule();
    release(); await Promise.all([one, two]);
    assert.equal(posts.length, 1);
});

test('the workout history page ignores a stale search', async () => {
    const src = read('workout_history.html');
    const doc = makeDocument();
    const waiting = [];
    const ctx = { document: doc, console, alert() {}, JSON, currentHistoryData: [], renderTable: (d) => { ctx.drawn = d; },
                  fetch: () => new Promise((resolve) => waiting.push(resolve)) };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'let historySeq', '    function renderTable') + '\nglobalThis.fetchHistory = fetchHistory;', ctx);
    const a = ctx.fetchHistory();
    const b = ctx.fetchHistory();
    waiting[1]({ ok: true, json: async () => [{ id: 'new' }] }); await b;
    waiting[0]({ ok: true, json: async () => [{ id: 'stale' }] }); await a;
    same(ctx.currentHistoryData, [{ id: 'new' }]);
});

test('the insights page says when the deep analytics could not be loaded', async () => {
    const py = fs.readFileSync(path.join(__dirname, '..', 'features.py'), 'utf8');
    const html = between(py, '_INSIGHTS_HTML = """', '"""\n');
    const script = between(html, '<script>', '</script>').slice('<script>'.length);
    const doc = makeDocument();
    const answers = { '/api/insights': { authenticated: true, deep_error: 'RuntimeError', data_note: 'Detailed analytics could not be loaded (RuntimeError)', unit: 'kg' },
                      '/api/recovery': { recommendation: 'train', message: 'ok' } };
    const ctx = { document: doc, console, JSON, Number, Object, Math, Array, fetch: async (u) => ({ ok: true, status: 200, json: async () => answers[u.split('?')[0]] }) };
    vm.createContext(ctx);
    vm.runInContext(script, ctx);
    await new Promise((r) => setTimeout(r, 20));
    assert.match(doc.getElementById('deep').innerHTML, /Detailed analytics could not be loaded \(RuntimeError\)/);
});

test('history: an older range that fails after a newer one loaded does not replace it', async () => {
    const src = read('history.html');
    const doc = makeDocument();
    const waiting = [];
    const ctx = { document: doc, console, alert() {}, window: { location: {} }, JSON, historyData: null,
                  fetch: () => new Promise((resolve, reject) => waiting.push({ resolve, reject })), renderHistory() {} };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'let historySeq', '    function renderHistory') + '\nglobalThis.fetchHistory = fetchHistory;', ctx);
    doc.getElementById('startDate').value = '2026-09-01'; doc.getElementById('endDate').value = '2026-09-30';
    const a = ctx.fetchHistory();
    const b = ctx.fetchHistory();
    waiting[1].resolve({ ok: true, status: 200, json: async () => ({ records: ['oct'] }) }); await b;
    doc.getElementById('emptyState').textContent = 'untouched';
    waiting[0].reject(new Error('network')); await a;
    assert.equal(doc.getElementById('emptyState').textContent, 'untouched');
});

test('the single-range export is named for the range that was loaded', () => {
    const src = read('history.html');
    const doc = makeDocument();
    const anchors = [];
    doc.createElement = () => { const a = { click() {} }; anchors.push(a); return a; };
    doc.getElementById('startDate').value = '2026-10-01'; doc.getElementById('endDate').value = '2026-10-31';  // what the inputs say NOW
    const ctx = { document: doc, console, JSON, alert() {}, Blob: class {}, URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
                  historyData: { records: [{ id: 1 }], stats: {} }, loadedRange: { start: '2026-09-01', end: '2026-09-30' } };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'function exportHistory', '    // Keyboard shortcuts') + '\nglobalThis.exportHistory = exportHistory;', ctx);
    ctx.exportHistory();
    assert.equal(anchors[0].download, 'workout_history_2026-09-01_2026-09-30.json');
});

// ----------------------------------------------------------------------------------------------- round 24
test('the dashboard export (a list of workouts) can be imported one workout at a time', async () => {
    const one = { name: 'x', weightUnit: 'kg', exercises: [{ id: 5, sets: [aSet()] }] };
    const e = editor(() => metaOf());
    e.ctx.userUnit = 0;
    await importJson(e.ctx, e.doc, [one]);
    assert.equal(e.ctx.workoutData.length, 1);
    const many = editor(() => metaOf());
    many.ctx.userUnit = 0;
    many.ctx.workoutData = planWith(1);
    await importJson(many.ctx, many.doc, [one, one]);
    assert.equal(many.ctx.workoutData[0].title, 'old0');
    assert.ok(many.calls.some((c) => c[0] === 'alert' && /holds 2 workouts/.test(c[1])));
    const empty = editor(() => metaOf());
    await importJson(empty.ctx, empty.doc, []);
    assert.ok(empty.calls.some((c) => c[0] === 'alert' && /holds 0 workouts/.test(c[1])));
});

test('history: a deep export in which every detail failed is not downloaded', async () => {
    const { ctx, calls } = historyActions('history.html', 'async function deepExportHistory', '    function exportHistory', {
        historyData: { records: [{ trainingId: 5, type: 5 }], start: 'a', end: 'b' }, loadedRange: { start: 'a', end: 'b' },
        fetch: async () => ({ ok: false, status: 502, json: async () => ({ error: 'history_unavailable' }) }),
    });
    await ctx.deepExportHistory();
    assert.equal(calls.filter((c) => c[0] === 'blob').length, 0);
    assert.ok(calls.some((c) => c[0] === 'alert' && /Export failed/.test(c[1])));
});

test('a measured zero is not replaced by the prescribed duration, and vendor numbers are escaped', () => {
    assert.match(read('history.html'), /const dur = \(s\.time \?\? s\.targetCount\) \|\| 0;/);
    assert.equal(((0 ?? 60) || 0), 0);
    assert.match(read('browse.html'), /esc\(Number\(c\.trainedUserCount\)\.toLocaleString\(\)\)/);
    assert.doesNotMatch(read('browse.html'), /\$\{c\.trainedUserCount\.toLocaleString\(\)\}/);
});

// ----------------------------------------------------------------------------------------------- round 25
test('adding a set to a Vita exercise repeats the current level, not the cable default', () => {
    const src = read('create.html');
    const ctx = { workoutData: [], getRules: (ex) => WorkoutLogic.getRules(ex, 1), getSetGoalUnit: (ex) => WorkoutLogic.getSetGoalUnit(ex), renderBuilder() {}, window: {} };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'window.addSet = ', '    window.removeSet') + '\n', ctx);
    ctx.workoutData = [{ selectedPresetId: -1, dataStatType: 6, sets: [{ reps: 8, weight: 3, rest: 30, unit: 'reps' }] },
                       { selectedPresetId: -1, dataStatType: 6, sets: [] },
                       { selectedPresetId: -1, dataStatType: null, sets: [{ reps: 8, weight: 20, rest: 30, unit: 'reps' }] }];
    ctx.window.addSet(0); ctx.window.addSet(1); ctx.window.addSet(2);
    assert.equal(ctx.workoutData[0].sets[1].weight, 3);
    assert.equal(ctx.workoutData[1].sets[0].weight, 5);
    assert.equal(ctx.workoutData[2].sets[1].weight, WorkoutLogic.getRules({ selectedPresetId: -1 }, 1).defW);
});

test('a bulk export skips a template whose detail is empty', () => {
    assert.equal(bulkExport({}), undefined);  // nothing to export: says so and stops
    assert.equal(bulkExport({ name: 'n', actionLibraryList: [] }), undefined);
});

test('the workout history page clears an old table when a search fails', async () => {
    const src = read('workout_history.html');
    const doc = makeDocument();
    const alerts = [];
    const ctx = { document: doc, console, alert: (m) => alerts.push(m), JSON, currentHistoryData: [{ id: 'old' }], renderTable() {},
                  esc: (s) => String(s), fetch: async () => ({ ok: true, json: async () => ({ error: 'history_unavailable' }) }) };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'let historySeq', '    function renderTable') + '\nglobalThis.fetchHistory = fetchHistory;', ctx);
    await ctx.fetchHistory();
    same(ctx.currentHistoryData, []);
    assert.match(doc.getElementById('history-table-body').innerHTML, /history_unavailable/);
});

test('history: a weight that arrives as text still renders', () => {
    const src = read('history.html');
    const ctx = {};
    vm.createContext(ctx);
    vm.runInContext(between(src, 'function fmtWeight', '    let historySeq') + '\nglobalThis.f = fmtWeight;', ctx);
    assert.equal(ctx.f('20'), 20);
    assert.equal(ctx.f('12.34'), '12.3');
    assert.equal(ctx.f(null), '-');
    assert.equal(ctx.f('abc'), '-');
});

test('a removal that throws after a good add refreshes the calendar and warns about both dates', async () => {
    const { ctx, calls, alerts } = calendar([{ success: true }, 'throw']);
    await move(ctx);
    same(calls, ['add:2099-01-02', 'remove:2099-01-01', 'refresh']);
    assert.match(alerts[0], /may show on both|both/);
});

test('the workout history page: an older failure does not replace a newer table', async () => {
    const src = read('workout_history.html');
    const doc = makeDocument();
    const waiting = [];
    const ctx = { document: doc, console: { error() {} }, alert() {}, JSON, currentHistoryData: [], renderTable: (d) => { doc.getElementById('history-table-body').innerHTML = 'rows:' + d.length; },
                  esc: (s) => String(s), fetch: () => new Promise((resolve, reject) => waiting.push({ resolve, reject })) };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'let historySeq', '    function renderTable') + '\nglobalThis.fetchHistory = fetchHistory;', ctx);
    const a = ctx.fetchHistory();
    const b = ctx.fetchHistory();
    waiting[1].resolve({ ok: true, json: async () => [{ id: 'new' }] }); await b;
    waiting[0].reject(new Error('offline')); await a;
    assert.equal(doc.getElementById('history-table-body').innerHTML, 'rows:1');
});

test('a bulk export is named for the local date, not the UTC one', () => {
    const src = read('index.html');
    const made = [];
    class FakeDate { getFullYear() { return 2026; } getMonth() { return 0; } getDate() { return 5; } toISOString() { return '2026-01-06T03:00:00.000Z'; } }
    const ctx = { Date: FakeDate, Blob: class {}, JSON, exportArr: [], URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
                  document: { createElement: () => { const a = { click() {} }; made.push(a); return a; } } };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'const jsonStr = JSON.stringify(exportArr', '            URL.revokeObjectURL(url);'), ctx);
    assert.equal(made[0].download, 'workouts_export_2026-01-05.json');
});

// ----------------------------------------------------------------------------------------------- round 26
test('a forced-timed exercise is stored, exported and kept as timed from the start', async () => {
    const { ctx } = editor(() => metaOf({ completionMethod: 2 }));
    const src = read('create.html');
    vm.runInContext(between(src, 'async function addExerciseToPlan', '    // --- ').replace('async function addExerciseToPlan', 'globalThis.addExerciseToPlan = async function addExerciseToPlan'), ctx);
    await ctx.addExerciseToPlan(7, 'Plank', '');
    same(ctx.workoutData[0].sets.map((s) => s.unit), ['sec', 'sec', 'sec']);
    ctx.workoutData[0].sets[0].reps = 60;
    vm.runInContext(between(src, 'window.updateEx = ', '    window.updateSet') + '\n', ctx);
    ctx.getRules = (ex) => WorkoutLogic.getRules(ex, 1);
    ctx.window.updateEx(0, 'selectedPresetId', '-1');
    assert.equal(ctx.workoutData[0].sets[0].reps, 60);  // a hold is not reset to the rep default
    const exported = JSON.parse(JSON.stringify(WorkoutLogic.buildExportJSON(ctx.workoutData, 'p', 1)));
    assert.equal(exported.exercises[0].sets[0].unit, 'sec');
    ctx.window.addSet = undefined;
    vm.runInContext(between(src, 'window.addSet = ', '    window.removeSet') + '\n', ctx);
    ctx.window.addSet(0);
    assert.equal(ctx.workoutData[0].sets.at(-1).unit, 'sec');
});

test('an imported exercise id that is not a number never becomes a fetch path', async () => {
    const src = read('create.html');
    const fetched = [];
    const ctx = { fetch: async (u) => { fetched.push(u); return { ok: true, json: async () => ({}) }; }, console };
    vm.createContext(ctx);
    vm.runInContext(between(src, 'async function fetchExerciseMetadata', '    let templateLoading') + '\nglobalThis.f = fetchExerciseMetadata;', ctx);
    for (const bad of ['../../x?y', '1/2', '', 'abc', '1e5']) assert.equal((await ctx.f(bad)).failed, true, bad);
    assert.deepEqual(fetched, []);
    await ctx.f('451234567890123');
    assert.deepEqual(fetched, ['/api/exercise/451234567890123']);
});

// ----------------------------------------------------------------------------------------------- round 27
test('only the newest import lands, whichever finishes last', async () => {
    const gates = [];
    const { ctx, doc } = editor(() => new Promise((resolve) => gates.push(resolve)));
    const first = importJson(ctx, doc, { name: 'a', exercises: [{ id: 5, sets: [{ reps: 10, weight: 100, rest: 60 }] }] });
    const second = importJson(ctx, doc, { name: 'b', exercises: [{ id: 6, sets: [{ reps: 10, weight: 20, rest: 60 }] }] });
    while (gates.length < 2) await new Promise((r) => setTimeout(r, 1));
    gates[1](metaOf()); await second;
    gates[0](metaOf()); await first;
    same(ctx.workoutData.map((e) => e.sets[0].weight), [20]);
});

test('an imported exercise takes its goal unit from the exercise, not from the file', async () => {
    const timed = editor(() => metaOf({ completionMethod: 2 }));
    await importJson(timed.ctx, timed.doc, { name: 'n', exercises: [{ id: 5, sets: [{ reps: 60, weight: 20 }] }] });
    assert.equal(timed.ctx.workoutData[0].sets[0].unit, 'sec');
    const repBased = editor(() => metaOf({ completionMethod: 1 }));
    await importJson(repBased.ctx, repBased.doc, { name: 'n', exercises: [{ id: 5, sets: [{ reps: 10, weight: 20, unit: 'sec' }] }] });
    assert.equal(repBased.ctx.workoutData[0].sets[0].unit, 'reps');
    const free = editor(() => metaOf());
    await importJson(free.ctx, free.doc, { name: 'n', exercises: [{ id: 5, sets: [{ reps: 60, weight: 20, unit: 'sec' }] }] });
    assert.equal(free.ctx.workoutData[0].sets[0].unit, 'sec');  // no forced unit: the file's choice stands
});

// ----------------------------------------------------------------------------------------------- round 28
test('a superseded import does not rename the plan', async () => {
    const gates = [];
    const { ctx, doc } = editor(() => new Promise((resolve) => gates.push(resolve)));
    const first = importJson(ctx, doc, { name: 'Old plan', exercises: [{ id: 5, sets: [{ reps: 10, weight: 100, rest: 60 }] }] });
    const second = importJson(ctx, doc, { name: 'New plan', exercises: [{ id: 6, sets: [{ reps: 10, weight: 20, rest: 60 }] }] });
    while (gates.length < 2) await new Promise((r) => setTimeout(r, 1));
    gates[1](metaOf()); await second;
    gates[0](metaOf()); await first;
    assert.equal(doc.getElementById('plan-name').value, 'New plan');
});

test('the set editor offers every mode the saver accepts', () => {
    const src = read('create.html');
    for (const [value, label] of [[1, 'Standard'], [2, 'Chains'], [3, 'Eccentric'], [4, 'Constant'], [5, 'Spotter']]) {
        assert.match(src, new RegExp(`<option value="${value}" \\$\\{set\\.mode == ${value} \\? 'selected' : ''\\}>${label}</option>`), label);
    }
});
