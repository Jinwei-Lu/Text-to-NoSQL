/* QueryCraft — Text-to-MQL demo for the TEND SAG solver.
   Every node is built with the DOM API: dataset values (field paths, keys, rows)
   are never interpolated into markup. */
(function () {
    'use strict';

    var API = {
        health: '/api/health',
        databases: '/api/databases',
        examples: function (db) { return '/api/examples/' + encodeURIComponent(db); },
        schema: function (db) { return '/api/schema/' + encodeURIComponent(db); },
        solve: '/api/solve',
        execute: '/api/execute'
    };
    var THEME_KEY = 'querycraft.theme';
    var MAX_COLUMNS = 24;
    var PREFERRED_DB = 'student_club';
    var SVG_NS = 'http://www.w3.org/2000/svg';

    var ICONS = {
        chevron: 'M9 6l6 6-6 6',
        alert: 'M12 4.5l8.5 15h-17z|M12 10v4M12 16.6v.01',
        arrow: 'M4 12h13M12.5 7.5L17 12l-4.5 4.5',
        sun: 'M12 7.6a4.4 4.4 0 1 0 0 8.8 4.4 4.4 0 0 0 0-8.8z|M12 2v2.2M12 19.8V22M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2 12h2.2M19.8 12H22M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6',
        moon: 'M20.5 14.3A8.5 8.5 0 1 1 10.2 3.6a7 7 0 0 0 10.3 10.7z'
    };

    // Stage families colour the flow chips and each stage's rail, so the shape of
    // a query (filter → reshape → aggregate → order) reads before the detail does.
    var STAGE_FAMILY = {
        $match: 'filter', $limit: 'filter', $skip: 'filter', $geoNear: 'filter',
        $project: 'reshape', $addFields: 'reshape', $set: 'reshape', $unset: 'reshape',
        $unwind: 'reshape', $replaceRoot: 'reshape', $replaceWith: 'reshape',
        $densify: 'reshape', $fill: 'reshape', $redact: 'reshape',
        $group: 'group', $count: 'group', $facet: 'group', $bucket: 'group',
        $bucketAuto: 'group', $setWindowFields: 'group', $sortByCount: 'group',
        $lookup: 'join', $unionWith: 'join', $graphLookup: 'join',
        $sort: 'order'
    };

    var state = {
        health: null,
        databases: [],
        dbId: '',
        schema: null,
        collection: '',
        examples: [],
        recordId: null,
        mode: 'stub',
        solve: null,
        pipeline: [],
        pipelineCollection: '',
        mql: '',
        mqlDirty: false,
        execution: null,
        views: { pipeline: 'stages', result: 'table', schema: 'shape' },
        sampleIndex: 0,
        reqSolve: 0,
        reqSchema: 0,
        busy: false
    };

    var dom = {};
    [
        'dbList', 'datasetChip', 'datasetChipText', 'healthDot', 'modeSeg', 'themeBtn',
        'askDbChip', 'recordChip', 'examplesBtn', 'examplesList', 'nlq', 'autoRun', 'status',
        'generateBtn', 'genKbd', 'pipelineCard', 'pipelineTabs', 'pipelineTarget', 'pipelineBody',
        'stageFlow', 'copyMqlBtn', 'runBtn', 'trace', 'resultTabs', 'resultMeta', 'resultBody',
        'copyRowsBtn', 'schemaToggle', 'schemaPanel', 'schemaSummary', 'schemaHint', 'schemaNote',
        'collectionList', 'schemaTabs', 'schemaBody', 'dynBadge', 'toast'
    ].forEach(function (id) { dom[id] = document.getElementById(id); });

    /* ── primitives ──────────────────────────────────────────────────── */

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) { node.className = cls; }
        if (text !== undefined && text !== null) { node.textContent = String(text); }
        return node;
    }

    function icon(name, cls) {
        var svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
        svg.setAttribute('aria-hidden', 'true');
        (ICONS[name] || '').split('|').forEach(function (d) {
            var path = document.createElementNS(SVG_NS, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        return svg;
    }

    function clear(node) {
        while (node.firstChild) { node.removeChild(node.firstChild); }
        return node;
    }

    function fill(node, child) {
        clear(node);
        if (child) { node.appendChild(child); }
        return node;
    }

    var numberFormat = new Intl.NumberFormat();

    function num(value) {
        var parsed = Number(value);
        return Number.isFinite(parsed) ? numberFormat.format(parsed) : '—';
    }

    function pluralize(count, word) {
        return num(count) + ' ' + word + (Number(count) === 1 ? '' : 's');
    }

    function truncate(text, max) {
        var value = String(text || '');
        return value.length > max ? value.slice(0, max - 1) + '…' : value;
    }

    function chip(text, cls) {
        return el('span', 'chip' + (cls ? ' ' + cls : ''), text);
    }

    function empty(message) {
        return el('div', 'empty', message);
    }

    function notice(message, kind) {
        var wrap = el('div', 'notice' + (kind ? ' notice--' + kind : ''));
        wrap.appendChild(icon('alert'));
        wrap.appendChild(el('div', null, message));
        return wrap;
    }

    var toastTimer = null;

    function toast(message) {
        dom.toast.textContent = message;
        dom.toast.hidden = false;
        window.clearTimeout(toastTimer);
        toastTimer = window.setTimeout(function () { dom.toast.hidden = true; }, 1600);
    }

    function copy(text, label) {
        var value = String(text || '');
        if (!value) { return; }
        if (!navigator.clipboard) {
            toast('Clipboard unavailable');
            return;
        }
        navigator.clipboard.writeText(value).then(
            function () { toast(label + ' copied'); },
            function () { toast('Copy failed'); }
        );
    }

    async function requestJson(url, options) {
        var response = await fetch(url, options);
        var data = null;
        try {
            data = await response.json();
        } catch (error) {
            throw new Error('The server returned a non-JSON response (' + response.status + ').');
        }
        if (!response.ok || (data && data.status === 'error')) {
            throw new Error((data && data.message) || ('Request failed (' + response.status + ').'));
        }
        return data;
    }

    function postJson(url, body) {
        return requestJson(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
    }

    /* ── theme ───────────────────────────────────────────────────────── */

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        fill(dom.themeBtn, icon(theme === 'dark' ? 'sun' : 'moon'));
        try { localStorage.setItem(THEME_KEY, theme); } catch (error) { /* private mode */ }
    }

    function initTheme() {
        var stored = null;
        try { stored = localStorage.getItem(THEME_KEY); } catch (error) { /* private mode */ }
        applyTheme(stored === 'dark' ? 'dark' : 'light');
        dom.themeBtn.addEventListener('click', function () {
            applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
        });
    }

    /* ── JSON tree ───────────────────────────────────────────────────── */

    var TRUNCATION_KEYS = {
        __truncated_keys__: function (value) { return '+' + num(value) + ' more keys'; },
        __truncated_items__: function (value) { return '+' + num(value) + ' more items'; },
        __truncated_array__: function (value) { return num(value) + ' items not sampled'; },
        __truncated_object__: function (value) {
            return (Array.isArray(value) ? value.length : 0) + ' keys not sampled';
        }
    };

    function keySpan(key) {
        var text = String(key);
        return el('span', 'jk' + (text.charAt(0) === '$' ? ' jk--op' : ''), JSON.stringify(text));
    }

    function scalarSpan(value) {
        if (value === null) { return el('span', 'jlit', 'null'); }
        var kind = typeof value;
        if (kind === 'number') { return el('span', 'jnum', String(value)); }
        if (kind === 'boolean') { return el('span', 'jlit', String(value)); }
        return el('span', 'jstr', JSON.stringify(String(value)));
    }

    function jsonEntry(value, key, depth, openDepth) {
        if (value === null || typeof value !== 'object') {
            var row = el('div', 'jrow');
            if (key !== null) {
                row.appendChild(keySpan(key));
                row.appendChild(el('span', 'jp', ': '));
            }
            row.appendChild(scalarSpan(value));
            return row;
        }

        var isArray = Array.isArray(value);
        var entries = isArray
            ? value.map(function (item) { return [null, item]; })
            : Object.keys(value).map(function (name) { return [name, value[name]]; });

        var node = el('div', 'jnode');
        if (depth < openDepth) { node.classList.add('is-open'); }

        var head = el('div', 'jrow');
        var caret = el('button', 'jcaret');
        caret.type = 'button';
        caret.setAttribute('aria-label', 'Toggle');
        caret.appendChild(icon('chevron'));
        caret.addEventListener('click', function () { node.classList.toggle('is-open'); });
        head.appendChild(caret);
        if (key !== null) {
            head.appendChild(keySpan(key));
            head.appendChild(el('span', 'jp', ': '));
        }
        head.appendChild(el('span', 'jp', isArray ? '[' : '{'));
        head.appendChild(el('span', 'jsummary',
            ' ' + entries.length + ' ' + (isArray ? 'item' : 'key') + (entries.length === 1 ? '' : 's') + ' '));
        head.appendChild(el('span', 'jsummary jp', isArray ? ']' : '}'));
        node.appendChild(head);

        var kids = el('div', 'jkids');
        entries.forEach(function (entry) {
            var childKey = entry[0];
            var childValue = entry[1];
            if (childKey !== null && TRUNCATION_KEYS[childKey]) {
                kids.appendChild(el('div', 'jrow jtrunc', TRUNCATION_KEYS[childKey](childValue)));
                return;
            }
            if (childKey === null && childValue && typeof childValue === 'object' && !Array.isArray(childValue)) {
                var names = Object.keys(childValue);
                if (names.length === 1 && TRUNCATION_KEYS[names[0]]) {
                    kids.appendChild(el('div', 'jrow jtrunc', TRUNCATION_KEYS[names[0]](childValue[names[0]])));
                    return;
                }
            }
            kids.appendChild(jsonEntry(childValue, childKey, depth + 1, openDepth));
        });
        node.appendChild(kids);
        node.appendChild(el('div', 'jrow jclose jp', isArray ? ']' : '}'));
        return node;
    }

    function jsonTree(value, openDepth, cls) {
        var wrap = el('div', 'json' + (cls ? ' ' + cls : ''));
        wrap.appendChild(jsonEntry(value, null, 0, openDepth === undefined ? 2 : openDepth));
        return wrap;
    }

    /* ── shape tree ──────────────────────────────────────────────────── */

    function typeText(node) {
        var types = Array.isArray(node.types) ? node.types : [];
        if (node.kind === 'dynamic_map') {
            return 'map · ' + pluralize(node.key_count || 0, 'key');
        }
        if (node.kind === 'array') {
            return 'array' + (node.array
                ? ' ' + num(node.array.min_length) + '–' + num(node.array.max_length)
                : '');
        }
        if (node.kind === 'object') {
            return 'object · ' + pluralize(node.child_count || (node.children || []).length, 'field');
        }
        var scalars = types.filter(function (name) { return name !== 'null'; });
        var label = scalars.length ? scalars.join(' | ') : (types[0] || 'unknown');
        return label + (types.indexOf('null') >= 0 ? ' | null' : '');
    }

    function shapeName(node) {
        var name = String(node.name || node.path || '');
        return name === '[]' ? '[ ]' : name;
    }

    function shapeNode(node, depth, openDepth) {
        var wrap = el('div', 'tnode');
        var children = Array.isArray(node.children) ? node.children : [];
        var samples = Array.isArray(node.key_samples) ? node.key_samples : [];
        var expandable = children.length > 0 || samples.length > 0;

        var row = el('div', 'trow');
        var caret = el('button', 'tcaret' + (expandable ? '' : ' tcaret--void'));
        caret.type = 'button';
        caret.setAttribute('aria-label', 'Toggle ' + shapeName(node));
        caret.appendChild(icon('chevron'));
        row.appendChild(caret);

        var dynamic = node.kind === 'dynamic_map' || /^\{.*\}$/.test(shapeName(node));
        var name = el('span', 'tname' + (dynamic ? ' tname--dyn' : ''), shapeName(node));
        name.title = String(node.path || node.name || '');
        row.appendChild(name);
        row.appendChild(el('span', 'ttype', typeText(node)));

        var percent = Number(node.presence_pct);
        if (Number.isFinite(percent) && percent < 99.95) {
            row.appendChild(el('span', 'tpct', 'in ' + Math.round(percent) + '%'));
        }
        wrap.appendChild(row);

        if (!expandable) { return wrap; }
        if (depth < openDepth) { wrap.classList.add('is-open'); }
        caret.addEventListener('click', function () { wrap.classList.toggle('is-open'); });

        var kids = el('div', 'tkids');
        if (samples.length) {
            var keyRow = el('div', 'keyrow');
            samples.slice(0, 8).forEach(function (key) {
                keyRow.appendChild(el('span', 'keychip', String(key)));
            });
            if (Number(node.key_count) > samples.length) {
                keyRow.appendChild(el('span', 'keychip', '+' + num(Number(node.key_count) - samples.length)));
            }
            kids.appendChild(keyRow);
        }
        children.forEach(function (child) { kids.appendChild(shapeNode(child, depth + 1, openDepth)); });
        if (node.truncated_children) {
            kids.appendChild(el('div', 'tmore', '+' + pluralize(node.truncated_children, 'more field') + ' not shown'));
        }
        wrap.appendChild(kids);
        return wrap;
    }

    function shapeTree(nodes, openDepth) {
        var tree = el('div', 'tree');
        nodes.forEach(function (node) { tree.appendChild(shapeNode(node, 0, openDepth || 1)); });
        return tree;
    }

    /* ── databases ───────────────────────────────────────────────────── */

    function renderDatabases() {
        var list = clear(dom.dbList);
        state.databases.forEach(function (db) {
            var item = el('button', 'db-item' + (db.db_id === state.dbId ? ' is-active' : ''));
            item.type = 'button';
            item.appendChild(el('span', null, db.db_id));
            if (db.collection_count != null) {
                item.appendChild(el('small', null, pluralize(db.collection_count, 'collection')));
            }
            item.addEventListener('click', function () {
                if (db.db_id !== state.dbId) { selectDatabase(db.db_id); }
            });
            list.appendChild(item);
        });
    }

    /* ── examples ────────────────────────────────────────────────────── */

    function renderExamples() {
        var list = clear(dom.examplesList);
        if (!state.examples.length) {
            list.appendChild(empty('No benchmark questions for this database.'));
            return;
        }
        state.examples.forEach(function (example) {
            var item = el('button', 'ex-item');
            item.type = 'button';
            item.appendChild(el('div', 'ex-id', 'TEND #' + example.record_id));
            item.appendChild(el('div', 'ex-text', example.NLQ || ''));
            item.addEventListener('click', function () {
                dom.nlq.value = example.NLQ || '';
                state.recordId = example.record_id;
                renderRecordChip();
                showExamples(false);
                dom.nlq.focus();
            });
            list.appendChild(item);
        });
    }

    function showExamples(show) {
        dom.examplesList.hidden = !show;
        dom.examplesBtn.setAttribute('aria-expanded', show ? 'true' : 'false');
    }

    function renderRecordChip() {
        var id = state.recordId;
        if (id === null || id === undefined || id === '') {
            dom.recordChip.hidden = true;
            return;
        }
        dom.recordChip.hidden = false;
        dom.recordChip.textContent = 'TEND #' + id;
    }

    /* ── schema ──────────────────────────────────────────────────────── */

    function activeCollection() {
        var collections = (state.schema && state.schema.collections) || [];
        for (var i = 0; i < collections.length; i++) {
            if (collections[i].name === state.collection) { return collections[i]; }
        }
        return collections[0] || null;
    }

    function renderSchemaSummary() {
        var schema = state.schema;
        if (!schema) {
            dom.schemaSummary.hidden = true;
            return;
        }
        var collections = schema.collections || [];
        var documents = collections.reduce(function (sum, item) {
            return sum + (Number(item.document_count) || 0);
        }, 0);
        var parts = [pluralize(collections.length, 'collection'), num(documents) + ' documents'];
        if (schema.dynamic_key_path_count) {
            parts.push(pluralize(schema.dynamic_key_path_count, 'dynamic key path'));
        }
        if (schema.max_depth) { parts.push('depth ' + num(schema.max_depth)); }
        dom.schemaSummary.hidden = false;
        dom.schemaSummary.textContent = parts.join(' · ');
    }

    function renderCollectionList() {
        var list = clear(dom.collectionList);
        var collections = (state.schema && state.schema.collections) || [];
        collections.forEach(function (collection) {
            var item = el('button', 'coll-item' + (collection.name === state.collection ? ' is-active' : ''));
            item.type = 'button';
            item.appendChild(el('span', null, collection.name));
            item.appendChild(el('small', null, num(collection.document_count)));
            if (collection.root_entity) { item.title = 'entity: ' + collection.root_entity; }
            item.addEventListener('click', function () {
                state.collection = collection.name;
                state.sampleIndex = 0;
                renderCollectionList();
                renderSchemaBody();
            });
            list.appendChild(item);
        });
    }

    function renderShapeView(collection) {
        var fields = Array.isArray(collection.top_level_fields) ? collection.top_level_fields : [];
        if (!fields.length) { return empty('No documents to infer a shape from.'); }
        return shapeTree(fields, 1);
    }

    function renderDynamicView(collection) {
        var maps = Array.isArray(collection.dynamic_maps) ? collection.dynamic_maps : [];
        if (!maps.length) {
            return empty('Every object in this collection uses fixed field names.');
        }
        var wrap = document.createDocumentFragment();
        wrap.appendChild(el('div', 'dyn-lead',
            'These paths are keyed by data. A query has to name the key it wants — '
            + 'the key set is not part of any declared schema.'));
        maps.forEach(function (map) {
            var card = el('div', 'dyn-card');
            var head = el('div', 'dyn-card-head');
            head.appendChild(el('div', 'dyn-path', map.value_path || map.path || ''));
            var meta = el('div', 'dyn-meta');
            meta.appendChild(chip(pluralize(map.key_count, 'observed key'), 'chip--dyn'));
            meta.appendChild(chip((map.value_kind || 'unknown') + ' values'));
            head.appendChild(meta);
            var samples = Array.isArray(map.key_samples) ? map.key_samples : [];
            if (samples.length) {
                var keys = el('div', 'keyrow');
                keys.style.padding = '10px 0 0';
                samples.forEach(function (key) { keys.appendChild(el('span', 'keychip', String(key))); });
                if (Number(map.key_count) > samples.length) {
                    keys.appendChild(el('span', 'keychip', '+' + num(Number(map.key_count) - samples.length)));
                }
                head.appendChild(keys);
            }
            card.appendChild(head);
            var shape = map.value_shape && typeof map.value_shape === 'object' ? map.value_shape : null;
            var children = shape && Array.isArray(shape.children) ? shape.children : [];
            card.appendChild(el('div', 'dyn-sub', 'Shape behind each key'));
            card.appendChild(children.length ? shapeTree(children, 1) : el('div', 'tmore', 'Scalar values.'));
            wrap.appendChild(card);
        });
        return wrap;
    }

    function renderSampleView(collection) {
        var samples = Array.isArray(collection.sample_documents) ? collection.sample_documents : [];
        if (!samples.length) { return empty('No sample document available.'); }
        return jsonTree(samples[Math.min(state.sampleIndex, samples.length - 1)], 2);
    }

    function renderSchemaBody() {
        var body = clear(dom.schemaBody);
        var collection = activeCollection();
        if (!collection) {
            dom.schemaNote.textContent = '';
            body.appendChild(empty('Select a database to inspect its documents.'));
            return;
        }
        state.collection = collection.name;
        var maps = Array.isArray(collection.dynamic_maps) ? collection.dynamic_maps : [];
        dom.dynBadge.textContent = String(maps.length);

        var view = state.views.schema;
        if (view === 'shape') {
            dom.schemaNote.textContent = 'inferred from '
                + pluralize(collection.sampled_shape_document_count, 'sampled document')
                + ' · percentages mark optional fields';
            body.appendChild(renderShapeView(collection));
            return;
        }
        if (view === 'dynamic') {
            dom.schemaNote.textContent = 'object keys that carry data instead of a fixed schema';
            body.appendChild(renderDynamicView(collection));
            return;
        }
        dom.schemaNote.textContent = 'one document, exactly as stored';
        body.appendChild(renderSampleView(collection));
    }

    /* ── pipeline ────────────────────────────────────────────────────── */

    function stageOperator(stage) {
        if (!stage || typeof stage !== 'object') { return '?'; }
        var keys = Object.keys(stage);
        return keys.length ? keys[0] : '?';
    }

    function stageFamily(operator) {
        return STAGE_FAMILY[operator] || 'other';
    }

    function stageHint(stage, operator) {
        var body = stage[operator];
        if (body === null || body === undefined) { return ''; }
        if (typeof body !== 'object') { return String(body); }
        if (Array.isArray(body)) { return body.length + ' entries'; }
        return Object.keys(body).slice(0, 3).join(', ');
    }

    function focusStage(index) {
        var target = dom.pipelineBody.querySelectorAll('.stage')[index];
        if (!target) { return; }
        target.classList.add('is-open');
        target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }

    function renderStageFlow(pipeline) {
        var flow = clear(dom.stageFlow);
        dom.stageFlow.hidden = !pipeline.length;
        pipeline.forEach(function (stage, index) {
            if (index) { flow.appendChild(icon('arrow', 'flow-arrow')); }
            var operator = stageOperator(stage);
            var op = el('button', 'flow-op fam-' + stageFamily(operator), operator);
            op.type = 'button';
            op.title = 'Stage ' + (index + 1);
            op.addEventListener('click', function () { focusStage(index); });
            flow.appendChild(op);
        });
    }

    function renderStages(pipeline) {
        var wrap = el('div', 'stages');
        var budget = 30;
        pipeline.forEach(function (stage, index) {
            var operator = stageOperator(stage);
            var body = stage[operator];
            var lines = JSON.stringify(body === undefined ? null : body, null, 2).split('\n').length;
            var open = lines <= 10 && budget >= lines;
            if (open) { budget -= lines; }

            var item = el('div', 'stage fam-' + stageFamily(operator) + (open ? ' is-open' : ''));
            var head = el('button', 'stage-head');
            head.type = 'button';
            head.appendChild(icon('chevron', 'stage-caret'));
            head.appendChild(el('span', 'stage-idx', index + 1));
            head.appendChild(el('span', 'stage-op', operator));
            head.appendChild(el('span', 'stage-hint', stageHint(stage, operator)));
            head.addEventListener('click', function () { item.classList.toggle('is-open'); });
            item.appendChild(head);
            var bodyNode = el('div', 'stage-body');
            bodyNode.appendChild(jsonTree(body, 4, 'json--flush'));
            item.appendChild(bodyNode);
            wrap.appendChild(item);
        });
        return wrap;
    }

    function prettyMql(collection, pipeline) {
        if (!collection || !Array.isArray(pipeline) || !pipeline.length) { return ''; }
        var stages = pipeline.map(function (stage) {
            return JSON.stringify(stage, null, 2).split('\n').map(function (line) {
                return '  ' + line;
            }).join('\n');
        });
        return 'db.' + collection + '.aggregate([\n' + stages.join(',\n') + '\n])';
    }

    function renderMqlEditor() {
        var area = el('textarea', 'mql-edit');
        area.spellcheck = false;
        area.value = state.mql;
        area.setAttribute('aria-label', 'Editable MQL pipeline');
        area.addEventListener('input', function () {
            state.mql = area.value;
            state.mqlDirty = true;
            dom.runBtn.disabled = state.busy || !state.mql.trim();
            dom.copyMqlBtn.disabled = !state.mql.trim();
        });
        return area;
    }

    function renderPipeline() {
        var body = clear(dom.pipelineBody);
        var result = (state.solve && state.solve.result) || null;
        var hasMql = Boolean(String(state.mql || '').trim());
        var pipeline = state.mqlDirty ? [] : state.pipeline;
        dom.copyMqlBtn.disabled = !hasMql;
        dom.runBtn.disabled = state.busy || !hasMql;

        if (state.pipelineCollection && !state.mqlDirty) {
            dom.pipelineTarget.hidden = false;
            dom.pipelineTarget.textContent = 'db.' + state.pipelineCollection;
        } else {
            dom.pipelineTarget.hidden = true;
        }
        renderStageFlow(pipeline);

        if (state.views.pipeline === 'mql') {
            body.appendChild(renderMqlEditor());
            return;
        }
        if (!result && !hasMql) {
            body.appendChild(empty('Ask a question above — the solver grounds it on the sampled '
                + 'document shapes and returns an aggregation pipeline.'));
            return;
        }
        if (result && result.result_type === 'solver_failure') {
            body.appendChild(notice((result.error_code || 'SOLVER_FAILURE') + ' — '
                + (result.message || 'no candidate produced.')));
        }
        if (!pipeline.length) {
            body.appendChild(hasMql
                ? notice('Edited by hand — press Run to parse and execute the MQL tab.', 'warn')
                : empty('The solver did not produce a pipeline.'));
            return;
        }
        body.appendChild(renderStages(pipeline));
    }

    /* ── trace ───────────────────────────────────────────────────────── */

    function metric(key, value, tone) {
        var wrap = el('div', 'metric');
        wrap.appendChild(el('span', 'metric-k', key));
        wrap.appendChild(el('span', 'metric-v' + (tone ? ' metric-v--' + tone : ''), value));
        return wrap;
    }

    function renderTrace() {
        var strip = clear(dom.trace);
        var solve = state.solve;
        dom.trace.hidden = !solve;
        if (!solve) { return; }
        var result = solve.result || {};
        strip.appendChild(metric('mode', solve.mode || '—'));
        strip.appendChild(metric('solver', result.solver_variant
            || (solve.policy && solve.policy.solver_variant) || '—'));
        strip.appendChild(metric('repair rounds', num(result.rounds || 0)));
        if (result.violations_final !== undefined) {
            strip.appendChild(metric('gate violations', num(result.violations_final),
                Number(result.violations_final) > 0 ? 'bad' : 'ok'));
        }
        if (result.empty_final) { strip.appendChild(metric('result', 'empty', 'warn')); }
        strip.appendChild(metric('time', (solve.elapsed_s || 0).toFixed(2) + 's'));
    }

    /* ── result ──────────────────────────────────────────────────────── */

    function cellFor(value) {
        if (value === null || value === undefined) { return el('span', 'cell-void', 'null'); }
        var kind = typeof value;
        if (kind === 'number') { return el('span', 'cell-num', num(value)); }
        if (kind === 'boolean') { return el('span', 'cell-lit', String(value)); }
        if (kind !== 'object') { return el('span', null, String(value)); }

        var isArray = Array.isArray(value);
        var size = isArray ? value.length : Object.keys(value).length;
        var wrap = el('div');
        var toggle = el('button', 'cell-nested', isArray ? '[ ' + size + ' ]' : '{ ' + size + ' }');
        toggle.type = 'button';
        var detail = el('div', 'cell-json');
        detail.hidden = true;
        detail.appendChild(jsonTree(value, 1, 'json--flush'));
        toggle.addEventListener('click', function () { detail.hidden = !detail.hidden; });
        wrap.appendChild(toggle);
        wrap.appendChild(detail);
        return wrap;
    }

    function renderTable(rows) {
        var columns = [];
        rows.forEach(function (row) {
            if (!row || typeof row !== 'object' || Array.isArray(row)) { return; }
            Object.keys(row).forEach(function (key) {
                if (columns.indexOf(key) < 0) { columns.push(key); }
            });
        });
        if (!columns.length) { return jsonTree(rows, 2); }
        var hidden = Math.max(0, columns.length - MAX_COLUMNS);
        columns = columns.slice(0, MAX_COLUMNS);

        var frag = document.createDocumentFragment();
        var wrap = el('div', 'table-wrap');
        var table = el('table', 'rows');
        var headRow = el('tr');
        headRow.appendChild(el('th', 'cell-idx', '#'));
        columns.forEach(function (key) { headRow.appendChild(el('th', null, key)); });
        var head = el('thead');
        head.appendChild(headRow);
        table.appendChild(head);

        var tbody = el('tbody');
        rows.forEach(function (row, index) {
            var tr = el('tr');
            tr.appendChild(el('td', 'cell-idx', index + 1));
            columns.forEach(function (key) {
                var value = row && typeof row === 'object' ? row[key] : undefined;
                var td = el('td', typeof value === 'number' ? 'cell-num' : null);
                td.appendChild(value === undefined ? el('span', 'cell-void', '—') : cellFor(value));
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        frag.appendChild(wrap);
        if (hidden) {
            frag.appendChild(el('div', 'table-note',
                '+' + pluralize(hidden, 'more column') + ' — see the JSON view'));
        }
        return frag;
    }

    function renderResultMeta(execution) {
        if (!execution || execution.status !== 'success') {
            dom.resultMeta.hidden = true;
            return;
        }
        var parts = [pluralize(execution.row_count, 'row')];
        if (execution.truncated) { parts.push('capped at ' + num(execution.row_limit)); }
        if (Number.isFinite(Number(execution.elapsed_s))) {
            parts.push(Number(execution.elapsed_s).toFixed(2) + 's');
        }
        dom.resultMeta.hidden = false;
        dom.resultMeta.textContent = parts.join(' · ');
    }

    function renderResult() {
        var body = clear(dom.resultBody);
        var execution = state.execution;
        renderResultMeta(execution);
        dom.copyRowsBtn.disabled = !(execution && execution.status === 'success'
            && (execution.rows || []).length);

        if (!execution) {
            body.appendChild(empty('Press Run to execute the pipeline read-only against MongoDB.'));
            return;
        }
        if (execution.status === 'skipped') {
            body.appendChild(notice('Execution skipped — ' + (execution.reason || 'unavailable.'), 'warn'));
            return;
        }
        if (execution.status === 'error') {
            body.appendChild(notice(execution.message || 'Execution failed.'));
            return;
        }
        var rows = Array.isArray(execution.rows) ? execution.rows : [];
        if (!rows.length) {
            body.appendChild(notice('The pipeline ran and returned 0 documents — usually a literal '
                + 'in $match that does not occur in the data.', 'warn'));
            return;
        }
        body.appendChild(state.views.result === 'json' ? jsonTree(rows, 2) : renderTable(rows));
    }

    /* ── actions ─────────────────────────────────────────────────────── */

    function setStatus(text, bad) {
        var status = clear(dom.status);
        dom.status.className = 'status' + (bad ? ' status--bad' : '');
        if (!text) { return; }
        if (state.busy) { status.appendChild(el('span', 'spin')); }
        status.appendChild(el('span', null, text));
    }

    function resetOutput() {
        state.solve = null;
        state.execution = null;
        state.pipeline = [];
        state.pipelineCollection = '';
        state.mql = '';
        state.mqlDirty = false;
        renderPipeline();
        renderTrace();
        renderResult();
    }

    function applyExecution(execution) {
        state.execution = execution || null;
        if (execution && execution.status === 'success' && Array.isArray(execution.pipeline)) {
            state.pipeline = execution.pipeline;
            state.pipelineCollection = execution.collection || state.pipelineCollection;
            state.mqlDirty = false;
            renderPipeline();
        }
        renderResult();
    }

    async function generate() {
        if (!state.dbId) {
            setStatus('Select a database first', true);
            return;
        }
        var question = dom.nlq.value.trim();
        if (!question) {
            dom.nlq.focus();
            setStatus('Type a question first', true);
            return;
        }
        var requestId = ++state.reqSolve;
        var failed = false;
        showExamples(false);
        resetOutput();
        state.busy = true;
        dom.generateBtn.disabled = true;
        setStatus(state.mode === 'live' ? 'Solving with the live model…' : 'Solving…');
        try {
            var data = await postJson(API.solve, {
                database: state.dbId,
                query: question,
                record_id: state.recordId,
                mode: state.mode,
                execute: dom.autoRun.checked
            });
            if (requestId !== state.reqSolve) { return; }
            state.solve = data;
            var result = data.result || {};
            state.pipeline = Array.isArray(result.pipeline) ? result.pipeline : [];
            state.pipelineCollection = result.collection || '';
            state.mql = prettyMql(state.pipelineCollection, state.pipeline) || String(result.MQL || '');
            state.mqlDirty = false;
            state.execution = data.execution || null;
            state.busy = false;
            renderPipeline();
            renderTrace();
            renderResult();
            dom.pipelineCard.scrollIntoView({ block: 'start', behavior: 'smooth' });
            if (result.result_type === 'solver_failure') {
                failed = true;
                setStatus(result.error_code || 'Solver failed', true);
            }
        } catch (error) {
            if (requestId !== state.reqSolve) { return; }
            failed = true;
            state.busy = false;
            fill(dom.pipelineBody, notice(error.message));
            setStatus('Request failed', true);
        } finally {
            if (requestId === state.reqSolve) {
                state.busy = false;
                dom.generateBtn.disabled = false;
                dom.runBtn.disabled = !String(state.mql || '').trim();
                if (!failed) { setStatus(''); }
            }
        }
    }

    async function run() {
        var mql = String(state.mql || '').trim();
        if (!mql || !state.dbId) { return; }
        dom.runBtn.disabled = true;
        fill(dom.resultBody, empty('Executing…'));
        try {
            var data = await postJson(API.execute, {
                database: state.dbId,
                mql: mql,
                mode: state.mode
            });
            applyExecution(data.execution);
        } catch (error) {
            state.execution = { status: 'error', message: error.message };
            renderResult();
        } finally {
            dom.runBtn.disabled = !String(state.mql || '').trim();
        }
    }

    async function loadSchema(dbId) {
        var requestId = ++state.reqSchema;
        state.schema = null;
        clear(dom.collectionList);
        dom.schemaSummary.hidden = true;
        fill(dom.schemaBody, empty('Sampling documents from ' + dbId + '…'));
        try {
            var data = await requestJson(API.schema(dbId));
            if (requestId !== state.reqSchema) { return; }
            state.schema = data.schema || null;
            var collections = (state.schema && state.schema.collections) || [];
            state.collection = collections.length ? collections[0].name : '';
            state.sampleIndex = 0;
            renderSchemaSummary();
            renderCollectionList();
            renderSchemaBody();
        } catch (error) {
            if (requestId !== state.reqSchema) { return; }
            fill(dom.schemaBody, notice(error.message));
        }
    }

    async function loadExamples(dbId) {
        state.examples = [];
        renderExamples();
        try {
            var data = await requestJson(API.examples(dbId));
            state.examples = Array.isArray(data.examples) ? data.examples : [];
        } catch (error) {
            state.examples = [];
        }
        renderExamples();
        var first = state.examples[0];
        dom.nlq.placeholder = first
            ? 'Ask anything, e.g. “' + truncate(first.NLQ, 140) + '”'
            : 'Ask anything about the selected database…';
    }

    async function selectDatabase(dbId) {
        state.dbId = dbId;
        state.recordId = null;
        dom.askDbChip.textContent = dbId;
        dom.nlq.value = '';
        renderRecordChip();
        renderDatabases();
        resetOutput();
        await Promise.all([loadSchema(dbId), loadExamples(dbId)]);
    }

    /* ── wiring ──────────────────────────────────────────────────────── */

    function applySegment(container, value, attribute) {
        Array.prototype.forEach.call(container.querySelectorAll('.seg-btn'), function (btn) {
            btn.classList.toggle('is-active', btn.dataset[attribute] === value);
        });
    }

    function bindEvents() {
        dom.generateBtn.addEventListener('click', generate);
        dom.runBtn.addEventListener('click', run);
        dom.copyMqlBtn.addEventListener('click', function () { copy(state.mql, 'Pipeline'); });
        dom.copyRowsBtn.addEventListener('click', function () {
            copy(JSON.stringify((state.execution && state.execution.rows) || [], null, 2), 'Rows');
        });

        dom.nlq.addEventListener('input', function () {
            state.recordId = null;
            renderRecordChip();
        });
        dom.nlq.addEventListener('keydown', function (event) {
            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                event.preventDefault();
                generate();
            }
        });

        dom.examplesBtn.addEventListener('click', function () {
            showExamples(dom.examplesList.hidden);
        });

        dom.modeSeg.addEventListener('click', function (event) {
            var btn = event.target.closest('.seg-btn');
            if (!btn) { return; }
            state.mode = btn.dataset.mode;
            applySegment(dom.modeSeg, state.mode, 'mode');
        });

        dom.pipelineTabs.addEventListener('click', function (event) {
            var btn = event.target.closest('.seg-btn');
            if (!btn) { return; }
            state.views.pipeline = btn.dataset.view;
            applySegment(dom.pipelineTabs, state.views.pipeline, 'view');
            renderPipeline();
        });

        dom.resultTabs.addEventListener('click', function (event) {
            var btn = event.target.closest('.seg-btn');
            if (!btn) { return; }
            state.views.result = btn.dataset.view;
            applySegment(dom.resultTabs, state.views.result, 'view');
            renderResult();
        });

        dom.schemaTabs.addEventListener('click', function (event) {
            var btn = event.target.closest('.seg-btn');
            if (!btn) { return; }
            state.views.schema = btn.dataset.view;
            applySegment(dom.schemaTabs, state.views.schema, 'view');
            renderSchemaBody();
        });

        dom.schemaToggle.addEventListener('click', function () {
            var open = dom.schemaPanel.hidden;
            dom.schemaPanel.hidden = !open;
            dom.schemaToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            dom.schemaHint.textContent = open ? 'hide' : 'show';
        });
    }

    /* ── boot ────────────────────────────────────────────────────────── */

    function renderDatasetChip() {
        var health = state.health;
        if (!health) {
            dom.datasetChipText.textContent = 'dataset unavailable';
            dom.healthDot.className = 'dot dot--bad';
            return;
        }
        dom.healthDot.className = 'dot dot--ok';
        dom.datasetChipText.textContent = health.dataset_dir + ' · '
            + pluralize(health.database_count, 'database') + ' · '
            + num(health.record_count) + ' questions';
    }

    async function boot() {
        initTheme();
        dom.genKbd.textContent = /Mac|iPhone|iPad|iPod/.test(navigator.platform
            || navigator.userAgent || '') ? '⌘↵' : 'Ctrl↵';
        renderExamples();
        renderPipeline();
        renderResult();
        renderSchemaBody();
        bindEvents();

        var results = await Promise.allSettled([
            requestJson(API.health),
            requestJson(API.databases)
        ]);
        state.health = results[0].status === 'fulfilled' ? results[0].value : null;
        renderDatasetChip();
        if (state.health && state.health.default_mode === 'live') {
            state.mode = 'live';
            applySegment(dom.modeSeg, 'live', 'mode');
        }
        if (results[1].status !== 'fulfilled') {
            setStatus(results[1].reason ? results[1].reason.message : 'Could not load databases', true);
            return;
        }
        state.databases = Array.isArray(results[1].value.databases) ? results[1].value.databases : [];
        renderDatabases();
        if (!state.databases.length) { return; }
        var preferred = state.databases.filter(function (db) { return db.db_id === PREFERRED_DB; })[0];
        await selectDatabase((preferred || state.databases[0]).db_id);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
}());
