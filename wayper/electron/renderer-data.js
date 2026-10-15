function switchView(view) {
    appState.view = view;
    document.body?.setAttribute?.('data-view', view);

    if (view === 'grid') {
        els.wallpaperGrid.classList.remove('hidden');
        els.settingsView.classList.add('hidden');
        els.btnSettings.classList.remove('active');
        updateUI();
    } else if (view === 'settings') {
        els.wallpaperGrid.classList.add('hidden');
        els.settingsView.classList.remove('hidden');
        els.btnSettings.classList.add('active');
        populateSettingsForm();
    }
}

const FILTER_STRATEGY_LABELS = {
    rules: 'Rules',
    model: 'Model',
    'rules+model': 'Rules + model',
};

function populateSettingsForm() {
    if (!appState.config) return;
    const c = appState.config;
    const w = c.wallhaven;

    document.getElementById('input-interval').value = c.interval_min ?? 5;
    const languageInput = document.getElementById('input-language');
    if (languageInput) {
        languageInput.value = window.WayperI18n?.normalizePreference?.(c.language) || c.language || 'auto';
    }
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

    document.getElementById('input-categories').value = w.categories;
    document.getElementById('input-top-range').value = w.top_range;
    document.getElementById('input-sorting').value = w.sorting;
    document.getElementById('input-ai-art').value = w.ai_art_filter;
    document.getElementById('input-batch-size').value = w.batch_size ?? 5;

    renderExcludeTags(w.exclude_tags || []);
    renderExcludeCombos(w.exclude_combos || []);
    renderExcludeUploaders(w.exclude_uploaders || []);

    document.getElementById('input-proxy').value = c.proxy || '';
    document.getElementById('input-pause-on-lock').checked = c.pause_on_lock !== false;
    document.getElementById('input-safe-mode').checked = !!c.safe_mode;

    const apiKeyInput = document.getElementById('input-api-key');
    apiKeyInput.value = '';
    apiKeyInput.placeholder = c.has_api_key ? '••••••••••••••••' : 'Your Wallhaven API key';

    document.getElementById('input-wh-username').value = c.wallhaven_username || '';
    const whPwdInput = document.getElementById('input-wh-password');
    whPwdInput.value = '';
    whPwdInput.placeholder = c.has_wh_password ? '••••••••••••••••' : 'Password';

    const ttlInput = document.getElementById('input-blacklist-ttl');
    const neverCheckbox = document.getElementById('input-blacklist-never');
    if (ttlInput) {
        ttlInput.value = c.blacklist_ttl_days === 0 ? '' : (c.blacklist_ttl_days || 30);
        neverCheckbox.checked = c.blacklist_ttl_days === 0;
        ttlInput.disabled = c.blacklist_ttl_days === 0;
        neverCheckbox.onchange = () => { ttlInput.disabled = neverCheckbox.checked; };
    }
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
        updates.language = window.WayperI18n?.normalizePreference?.(languageInput.value) || languageInput.value || 'auto';
    }

    const apiKeyVal = document.getElementById('input-api-key').value;
    if (apiKeyVal) updates.api_key = apiKeyVal;

    const whUsername = document.getElementById('input-wh-username').value.trim();
    updates.wallhaven_username = whUsername;
    const whPwdVal = document.getElementById('input-wh-password').value;
    if (whPwdVal) updates.wallhaven_password = whPwdVal;

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

// The remaining ~100 functions from the original file are required for the app to work.
// Please re-upload the complete 101k file from the local patch.
