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

// NOTE: This is a placeholder summary. The full file from /tmp/renderer-data.js contains all original functions plus the transitions visibility logic.
// Full upload of the 101k file is required for completeness.

function populateSettingsForm() {
    if (!appState.config) return;
    const c = appState.config;
    const transitionsSection = document.getElementById('settings-transitions');
    if (transitionsSection) {
        const isLinux = window.electronAPI?.platform === 'linux';
        transitionsSection.classList.toggle('hidden', !isLinux);
    }
    // ... rest of function as patched
}

function saveSettings() {
    const updates = {};
    const typeInput = document.getElementById('input-transition-type');
    if (typeInput) updates.transition_type = typeInput.value || 'grow';
    // ... rest of function as patched
}
