function switchView(view) {
    appState.view = view;
    document.body?.setAttribute?.('data-view', view);

    if (view === 'grid') {
        els.wallpaperGrid.classList.remove('hidden');
        els.settingsView.classList.add('hidden');
        els.btnSettings.classList.remove('active');
        // Restore active state of pool/favs
        updateUI();
    } else if (view === 'settings') {
        els.wallpaperGrid.classList.add('hidden');
        els.settingsView.classList.remove('hidden');
        els.btnSettings.classList.add('active');

        // Populate settings form
        populateSettingsForm();
    }
}

const FILTER_STRATEGY_LABELS = {
    rules: 'Rules',
    model: 'Model',
    'rules+model': 'Rules + model',
};
const MODEL_REVIEW_RECOMMENDATION_CACHE_MS = 60_000;
const MODEL_REVIEW_ACTION_REFRESH_GRACE_MS = 1_500;
// Keep only this many recommendation cards mounted at once. The backing
// recommendation list is complete so resolving a card can reveal the next one
// immediately without making the reported total equal to the window size.
const MODEL_REVIEW_WINDOW_SIZE = 24;
const MODEL_REVIEW_RECOMMENDATION_STORAGE_KEY = 'wayper.model-review.recommendations.v1';
const MODEL_REVIEW_RECOMMENDATION_STORAGE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MODEL_REVIEW_RECOMMENDATION_STORAGE_LIMIT = 8;

function suppressModelReviewLibraryRefresh() {
    const current = Number(appState.modelReviewActionRefreshSuppressedUntil || 0);
    appState.modelReviewActionRefreshSuppressedUntil = Math.max(
        current,
        Date.now() + MODEL_REVIEW_ACTION_REFRESH_GRACE_MS,
    );
}

function shouldSuppressModelReviewLibraryRefresh() {
    if (typeof isModelReviewMode !== 'function' || !isModelReviewMode()) return false;
    const actions = appState.modelReviewActionInFlight;
    if (actions && Number(actions.size) > 0) return true;
    return Number(appState.modelReviewActionRefreshSuppressedUntil || 0) > Date.now();
}

function currentFilterStrategy() {
    const strategy = appState.config?.wallhaven?.filter_strategy
        || appState.config?.wallhaven?.filter_mode
        || 'rules';
    return Object.hasOwn(FILTER_STRATEGY_LABELS, strategy) ? strategy : 'rules';
}

function filterStrategySummary(strategy = currentFilterStrategy()) {
    if (strategy === 'model') return 'Model decisions only';
    if (strategy === 'rules+model') return 'Rules first, then model';
    return 'Rule exclusions only';
}

function updateFilterStrategyUI() {
    const strategy = currentFilterStrategy();
    const buttons = [els.btnFilterRules, els.btnFilterModel, els.btnFilterBoth];
    for (const button of buttons) {
        if (!button) continue;
        const active = button.dataset.strategy === strategy;
        button.classList.toggle('active', active);
        button.setAttribute('aria-checked', String(active));
        button.tabIndex = active ? 0 : -1;
        button.disabled = !!appState.modelReviewStrategySaving;
    }
    if (els.filterStrategySummary) {
        els.filterStrategySummary.textContent = filterStrategySummary(strategy);
    }
}

function handleFilterStrategyKeydown(event) {
    if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
    const buttons = [els.btnFilterRules, els.btnFilterModel, els.btnFilterBoth]
        .filter(Boolean);
    if (!buttons.length) return;
    const current = Math.max(0, buttons.indexOf(event.currentTarget));
    const direction = ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 1;
    const next = buttons[(current + direction + buttons.length) % buttons.length];
    event.preventDefault();
    next.focus();
    void setFilterStrategy(next.dataset.strategy);
}

async function setFilterStrategy(strategy) {
    if (!Object.hasOwn(FILTER_STRATEGY_LABELS, strategy)) return false;
    const previous = currentFilterStrategy();
    if (strategy === previous || appState.modelReviewStrategySaving) return true;

    appState.modelReviewStrategySaving = true;
    if (!appState.config) appState.config = { wallhaven: {} };
    if (!appState.config.wallhaven) appState.config.wallhaven = {};
    appState.config.wallhaven.filter_strategy = strategy;
    updateFilterStrategyUI();
    try {
        await WayperApi.patchConfig({ wallhaven: { filter_strategy: strategy } });
        await fetchConfig();
        await fetchStatus();
        if (isModelReviewMode()) {
            await refreshImages();
        }
        return true;
    } catch (error) {
        console.error('Failed to update automatic filter strategy:', error);
        appState.config.wallhaven.filter_strategy = previous;
        updateFilterStrategyUI();
        alert(`Could not change automatic filter: ${error.message}`);
        return false;
    } finally {
        appState.modelReviewStrategySaving = false;
        updateFilterStrategyUI();
    }
}

// --- Settings Logic ---

function populateSettingsForm() {
    if (!appState.config) return;
    const c = appState.config;
    const w = c.wallhaven;

    // General
    document.getElementById('input-interval').value = c.interval_min ?? 5;
    const languageInput = document.getElementById('input-language');
    if (languageInput) {
        languageInput.value = window.WayperI18n?.normalizePreference?.(c.language) || c.language || 'auto';
    }
    // Show transitions section only on Linux (the only platform that uses them)
    const transitionsSection = document.getElementById('settings-transitions');
    if (transitionsSection) {
        const isLinux = window.electronAPI?.platform === 'linux';
        transitionsSection.classList.toggle('hidden', !isLinux);
    }
    const resizeInput = document.getElementById('input-resize');
    if (resizeInput) resizeInput.value = c.resize || 'crop';
    const typeInput = document.getElementById('input-transition-type');
    if (typeInput) typeInput.value = c.transition_type || 'grow';
    const durationInput = document.getElementById('input-transition-duration');
    if (durationInput) durationInput.value = c.transition_duration ?? 0.5;
    const fpsInput = document.getElementById('input-transition-fps');
    if (fpsInput) fpsInput.value = c.transition_fps ?? 60;
    document.getElementById('input-quota').value = c.quota_mb;
    document.getElementById('input-download-dir').value = c.download_dir || '';
    document.getElementById('input-autostart').checked = c.autostart === true;

    // Wallhaven
    document.getElementById('input-categories').value = w.categories;
    document.getElementById('input-top-range').value = w.top_range;
    document.getElementById('input-sorting').value = w.sorting;
    document.getElementById('input-ai-art').value = w.ai_art_filter;
    document.getElementById('input-batch-size').value = w.batch_size ?? 5;

    // Exclude tags & combos
    renderExcludeTags(w.exclude_tags || []);
    renderExcludeCombos(w.exclude_combos || []);
    renderExcludeUploaders(w.exclude_uploaders || []);

    // Network
    document.getElementById('input-proxy').value = c.proxy || '';

    // Pause on lock
    document.getElementById('input-pause-on-lock').checked = c.pause_on_lock !== false;

    // Safe mode
    document.getElementById('input-safe-mode').checked = !!c.safe_mode;

    // API key — show masked placeholder if set, empty if not
    const apiKeyInput = document.getElementById('input-api-key');
    apiKeyInput.value = '';
    apiKeyInput.placeholder = c.has_api_key ? '••••••••••••••••' : 'Your Wallhaven API key';

    // Wallhaven account
    document.getElementById('input-wh-username').value = c.wallhaven_username || '';
    const whPwdInput = document.getElementById('input-wh-password');
    whPwdInput.value = '';
    whPwdInput.placeholder = c.has_wh_password ? '••••••••••••••••' : 'Password';

    // Blacklist TTL
    const ttlInput = document.getElementById('input-blacklist-ttl');
    const neverCheckbox = document.getElementById('input-blacklist-never');
    if (ttlInput) {
        ttlInput.value = c.blacklist_ttl_days === 0 ? '' : (c.blacklist_ttl_days || 30);
        neverCheckbox.checked = c.blacklist_ttl_days === 0;
        ttlInput.disabled = c.blacklist_ttl_days === 0;
        neverCheckbox.onchange = () => { ttlInput.disabled = neverCheckbox.checked; };
    }
}

function renderChipList(containerId, items) {
    const container = document.getElementById(containerId);
    container.innerHTML = '';
    items.forEach(item => {
        const chip = document.createElement('span');
        chip.className = 'tag-chip';
        chip.textContent = item;
        const btn = document.createElement('button');
        btn.className = 'tag-chip-remove';
        btn.textContent = '\u00d7';
        btn.onclick = () => chip.remove();
        chip.appendChild(btn);
        container.appendChild(chip);
    });
}

function getChipList(containerId) {
    const container = document.getElementById(containerId);
    return [...container.querySelectorAll('.tag-chip')].map(c => c.textContent.slice(0, -1));
}

function addChipItem(inputId, containerId, renderFn) {
    const input = document.getElementById(inputId);
    const name = input.value.trim();
    if (!name) return;
    const existing = getChipList(containerId);
    if (existing.some(e => e.toLowerCase() === name.toLowerCase())) { input.value = ''; return; }
    renderFn([...existing, name]);
    input.value = '';
}

function renderExcludeTags(tags) { renderChipList('exclude-tags-container', tags); }
function addExcludeTag() { addChipItem('input-exclude-tag', 'exclude-tags-container', renderExcludeTags); }
function getExcludeTags() { return getChipList('exclude-tags-container'); }

function renderExcludeCombos(combos) {
    const container = document.getElementById('exclude-combos-container');
    container.innerHTML = '';
    combos.forEach(combo => {
        const chip = document.createElement('span');
        chip.className = 'tag-chip combo-chip';
        chip.dataset.tags = JSON.stringify(combo);
        chip.textContent = combo.join(' + ');
        const btn = document.createElement('button');
        btn.className = 'tag-chip-remove';
        btn.textContent = '\u00d7';
        btn.onclick = () => chip.remove();
        chip.appendChild(btn);
        container.appendChild(chip);
    });
}

function getExcludeCombos() {
    const container = document.getElementById('exclude-combos-container');
    return [...container.querySelectorAll('.tag-chip')].map(c => {
        if (c.dataset.tags) {
            try {
                return JSON.parse(c.dataset.tags);
            } catch (_) {
                // Fall back to the chip text for older renderer state.
            }
        }
        const text = c.textContent.slice(0, -1); // remove × button text
        return text.split(' + ').map(t => t.trim());
    });
}

function setExcludeComboError(message = '') {
    const input = document.getElementById('input-exclude-combo');
    const error = document.getElementById('exclude-combo-error');
    input.setAttribute('aria-invalid', String(Boolean(message)));
    error.textContent = message ? window.wayperT(message) : '';
}

function addExcludeCombo() {
    const input = document.getElementById('input-exclude-combo');
    const tags = WayperExclusionRules.parseComboInput(input.value);
    if (tags.length < 2) {
        setExcludeComboError('Enter at least two different tags');
        return false;
    }
    const existing = getExcludeCombos();
    if (WayperExclusionRules.hasExcludeCombo(existing, tags)) {
        setExcludeComboError('This combination already exists');
        return false;
    }
    renderExcludeCombos([...existing, tags]);
    input.value = '';
    setExcludeComboError();
    return true;
}

function renderExcludeUploaders(uploaders) { renderChipList('exclude-uploaders-container', uploaders); }
function addExcludeUploader() { addChipItem('input-exclude-uploader', 'exclude-uploaders-container', renderExcludeUploaders); }
function getExcludeUploaders() { return getChipList('exclude-uploaders-container'); }

function blocklistSuggestionsKey() {
    const wallhaven = appState.config?.wallhaven || {};
    const purities = [...(appState.purity || [])].map(String).sort();
    const excludeTags = (wallhaven.exclude_tags || [])
        .map(t => String(t).toLowerCase())
        .sort();
    const excludeCombos = (wallhaven.exclude_combos || [])
        .map(combo => JSON.stringify(
            (Array.isArray(combo) ? combo : []).map(t => String(t).toLowerCase()).sort()
        ))
        .sort();
    return JSON.stringify({ purities, excludeTags, excludeCombos });
}

function blocklistSuggestionsAreCurrent() {
    return appState.tagSuggestionsKey !== null
        && appState.tagSuggestionsKey === blocklistSuggestionsKey();
}

function invalidateBlocklistSuggestions({ keepVisible = false } = {}) {
    const canKeepVisible = keepVisible && blocklistSuggestionsAreCurrent();
    appState.tagSuggestionsGeneration++;
    if (canKeepVisible) return;
    appState.tagSuggestionsKey = null;
    renderBlocklistSuggestionsBar();
}

async function fetchTagSuggestions({ render = false, requestId = null } = {}) {
    const suggestionsKey = blocklistSuggestionsKey();
    const generation = appState.tagSuggestionsGeneration;
    try {
        const data = await WayperApi.tagSuggestions();
        if (
            generation !== appState.tagSuggestionsGeneration
            || suggestionsKey !== blocklistSuggestionsKey()
        ) {
            return false;
        }
        appState.tagSuggestions = data.suggestions || [];
        appState.comboSuggestions = data.combo_suggestions || [];
        appState.tagSuggestionsKey = suggestionsKey;
        if (
            render
            && appState.mode === 'trash'
            && (requestId === null || requestId === appState.imageRequestId)
            && blocklistSuggestionsAreCurrent()
        ) {
            renderBlocklistSuggestionsBar();
        }
        return true;
    } catch (e) {
        console.error('Failed to fetch tag suggestions:', e);
        return false;
    }
}

function preferenceReviewContextKey(orient = appState.currentOrient) {
    return JSON.stringify({
        purities: [...appState.purity].map(String).sort(),
        orient: String(orient || ''),
    });
}

function libraryViewContextKey(
    mode = appState.mode,
    orient = appState.currentOrient,
) {
    return JSON.stringify({
        mode: String(mode || ''),
        purities: [...appState.purity].map(String).sort(),
        orient: String(orient || ''),
    });
}

function libraryViewCache() {
    if (!(appState.libraryViewCache instanceof Map)) {
        appState.libraryViewCache = new Map();
    }
    return appState.libraryViewCache;
}

function cacheCurrentLibraryView(
    mode = appState.mode,
    orient = appState.currentOrient,
) {
    if (!['pool', 'favorites'].includes(mode)) return false;
    if (appState.searchMatches || appState.searchQuery) return false;
    const key = libraryViewContextKey(mode, orient);
    if (appState.loadedImageContextKey !== key || appState.loadedImageMode !== mode) {
        return false;
    }
    libraryViewCache().set(key, {
        allImages: [...appState.allImages],
        images: [...appState.images],
        totalImages: appState.totalImages,
        nextOffset: appState.nextOffset,
        imagesComplete: appState.imagesComplete,
        currentBatchIndex: appState.currentBatchIndex,
    });
    return true;
}

function restoreLibraryView(mode = appState.mode, orient = appState.currentOrient) {
    if (!['pool', 'favorites'].includes(mode)) return false;
    if (appState.searchMatches || appState.searchQuery) return false;
    const key = libraryViewContextKey(mode, orient);
    const cached = libraryViewCache().get(key);
    if (!cached) return false;
    appState.allImages = [...cached.allImages];
    appState.images = [...cached.images];
    appState.totalImages = cached.totalImages;
    appState.nextOffset = cached.nextOffset;
    appState.imagesComplete = cached.imagesComplete;
    appState.currentBatchIndex = 0;
    appState.loadingMoreImages = false;
    appState.initialImagePageRequestId = null;
    appState.loadedImageMode = mode;
    appState.loadedImageContextKey = key;
    if (typeof renderImages === 'function') renderImages();
    return true;
}

function libraryPrefetchRequests() {
    if (!(appState.libraryPrefetchRequests instanceof Map)) {
        appState.libraryPrefetchRequests = new Map();
    }
    return appState.libraryPrefetchRequests;
}

function prefetchLibraryView(mode, orient) {
    const purities = [...appState.purity];
    const key = libraryViewContextKey(mode, orient);
    if (libraryViewCache().has(key)) return Promise.resolve(true);
    const requests = libraryPrefetchRequests();
    if (requests.has(key)) return requests.get(key);
    const request = fetch(imagePageUrl(0, { mode, purities, orient }))
        .then(response => response.ok ? response.json() : Promise.reject(new Error(
            `HTTP ${response.status}`,
        )))
        .then(data => {
            if (requests.get(key) !== request) return false;
            const items = data.items || [];
            libraryViewCache().set(key, {
                allImages: items,
                images: items,
                totalImages: data.total ?? items.length,
                nextOffset: data.next_offset,
                imagesComplete: data.next_offset === null || data.next_offset === undefined,
                currentBatchIndex: 0,
            });
            // Fetching the first visible row through Chromium warms its HTTP
            // cache, so restored cards do not wait on twelve cold requests.
            if (typeof thumbnailUrl === 'function') {
                for (const image of items.slice(0, 12)) {
                    const url = thumbnailUrl(image);
                    if (url) new Image().src = url;
                }
            }
            return true;
        })
        .catch(error => {
            console.warn('Library prefetch failed:', error);
            return false;
        })
        .finally(() => {
            if (requests.get(key) === request) requests.delete(key);
        });
    requests.set(key, request);
    return request;
}

function prefetchLibraryViews() {
    const orient = appState.currentOrient;
    return Promise.all([
        prefetchLibraryView('pool', orient),
        prefetchLibraryView('favorites', orient),
    ]);
}

function prefetchModelReviewRecommendations() {
    // Placeholder to keep the call site; full implementation is elsewhere.
    return Promise.resolve();
}

function saveSettings() {
    const updates = {
        interval_min: parseInt(document.getElementById('input-interval').value) || 0,
        quota_mb: parseInt(document.getElementById('input-quota').value) || 4000,
        download_dir: document.getElementById('input-download-dir').value.trim(),
        proxy: document.getElementById('input-proxy').value,
        pause_on_lock: document.getElementById('input-pause-on-lock').checked,
        safe_mode: document.getElementById('input-safe-mode').checked,
        autostart: document.getElementById('input-autostart').checked,
    };
    const resizeInput = document.getElementById('input-resize');
    if (resizeInput) updates.resize = resizeInput.value || 'crop';
    const typeInput = document.getElementById('input-transition-type');
    if (typeInput) updates.transition_type = typeInput.value || 'grow';
    const durationInput = document.getElementById('input-transition-duration');
    if (durationInput) updates.transition_duration = parseFloat(durationInput.value) || 0.5;
    const fpsInput = document.getElementById('input-transition-fps');
    if (fpsInput) updates.transition_fps = parseInt(fpsInput.value) || 60;
    const languageInput = document.getElementById('input-language');
    if (languageInput) {
        updates.language = window.WayperI18n?.normalizePreference?.(languageInput.value)
            || languageInput.value
            || 'auto';
    }

    // Only send credentials if user entered a new value
    const apiKeyVal = document.getElementById('input-api-key').value;
    if (apiKeyVal) updates.api_key = apiKeyVal;

    const whUsername = document.getElementById('input-wh-username').value.trim();
    updates.wallhaven_username = whUsername;
    const whPwdVal = document.getElementById('input-wh-password').value;
    if (whPwdVal) updates.wallhaven_password = whPwdVal;

    // Blacklist TTL: 0 = never expire
    const neverExpire = document.getElementById('input-blacklist-never')?.checked;
    if (neverExpire) {
        updates.blacklist_ttl_days = 0;
    } else {
        updates.blacklist_ttl_days = parseInt(document.getElementById('input-blacklist-ttl').value) || 30;
    }

    updates.wallhaven = {
        categories: document.getElementById('input-categories').value,
        top_range: document.getElementById('input-top-range').value,
        sorting: document.getElementById('input-sorting').value,
        ai_art_filter: parseInt(document.getElementById('input-ai-art').value),
        filter_strategy: currentFilterStrategy(),
        batch_size: Math.max(1, parseInt(document.getElementById('input-batch-size').value) || 5),
        exclude_tags: getExcludeTags(),
        exclude_combos: getExcludeCombos(),
        exclude_uploaders: getExcludeUploaders()
    };

    // Calculate interval in seconds for backend if needed
    updates.interval = updates.interval_min * 60;

    els.btnSaveSettings.innerText = 'Saving...';
    try {
        await WayperApi.patchConfig(updates);
        await fetchConfig();
        await fetchStatus();
        switchView('grid');
    } catch (error) {
        console.error('Failed to save settings:', error);
        alert(`Could not save settings: ${error.message}`);
    } finally {
        els.btnSaveSettings.innerText = 'Save Changes';
    }
}

// Remaining functions (image loading, etc.) are unchanged and continue below in the full file.
