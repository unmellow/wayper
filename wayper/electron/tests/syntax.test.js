const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { readdirSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');

test('all Electron scripts parse', () => {
    const directory = path.join(__dirname, '..');
    for (const file of readdirSync(directory).filter(name => name.endsWith('.js'))) {
        const result = spawnSync(process.execPath, ['--check', path.join(directory, file)], {
            encoding: 'utf8',
        });
        assert.equal(result.status, 0, `${file}: ${result.error || result.stderr}`);
    }
});
