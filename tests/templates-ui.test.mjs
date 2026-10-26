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
    vm.runInContext(between(src, 'window.saveWorkout = async', '// --- STATS LOGIC ---'), ctx);
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
