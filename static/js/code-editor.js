/**
 * Monaco Editor wrapper for OJ submit page.
 * https://github.com/microsoft/monaco-editor
 *
 * Changes
 * -------
 * - ``self.MonacoEnvironment`` is configured BEFORE the loader script
 *   is injected. This tells Monaco where to fetch its language service
 *   workers (editorSimpleWorker, cssWorker, htmlWorker, tsWorker) so
 *   that the workers live on the same CDN as the editor itself and
 *   do not fall back to a 404 under the page's own origin.
 * - Non-JS/TS languages (Rust, Go, C, C++, ...) get their
 *   ``semanticTokensProvider`` / ``documentFormattingEditProvider``
 *   rejected at worker construction time; we additionally disable
 *   ``validate`` / ``semantic tokens`` for them to avoid spurious
 *   red squiggles.
 * - If Monaco's built-in ``rust`` contribution is missing (sometimes the
 *   case for certain mirrors), we register a minimal Monarch tokenizer
 *   as a fallback so the user still gets sane syntax highlighting.
 */
(function (global) {
    const MONACO_VERSION = '0.52.2';

    // List of fallback mirrors. The FIRST entry is /monaco/min/vs — a
    // same-origin URL served through Nginx reverse proxy to jsdelivr.
    // This bypasses cross-origin worker restrictions entirely (no need
    // for Blob/objectUrl hacks). Remaining entries act as fallbacks if
    // the primary source fails on load.
    const MONACO_BASE_CANDIDATES = [
        // Always try same-origin path first — served by Nginx
        // reverse proxy to jsdelivr. Avoids cross-origin Worker
        // issues (Blob + importScripts from a different protocol).
        '/monaco/min/vs',
        // Then try the admin-configured base (if any)
        (global.OJ_MONACO_BASE || '').toString().trim(),
        `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`,
        `https://unpkg.com/monaco-editor@${MONACO_VERSION}/min/vs`,
        `https://cdn.bootcdn.net/ajax/libs/monaco-editor/${MONACO_VERSION}/min/vs`,
        `https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/${MONACO_VERSION}/min/vs`,
    ].filter(Boolean);
    // De-duplicate while preserving order.
    const seen = new Set();
    const MONACO_BASES = [];
    for (const url of MONACO_BASE_CANDIDATES) {
        const key = url.replace(/\/+$/, '').toLowerCase();
        if (!seen.has(key)) {
            seen.add(key);
            MONACO_BASES.push(url);
        }
    }

    // ─────────────────────────────────────────────────────────────────
    // Configure MonacoEnvironment BEFORE the loader runs.
    //
    // We implement getWorker (highest priority in Monaco's resolution
    // order) so we have FULL control over the worker script:
    //
    //   1. We build a Blob that:
    //      - Sets `globalThis.MonacoEnvironment.baseUrl` to the right
    //        base so `require('vs/language/typescript/tsWorker')` resolves
    //        to `<base>/language/typescript/tsWorker.js` (NOT a
    //        double-`vs/vs/` path that would happen with the
    //        `getWorkerUrl` + `"../../../"` fallback path).
    //      - `importScripts(base + '/base/worker/workerMain.js')` to
    //        load Monaco's universal worker entrypoint.
    //
    //   2. The same blob is reused for every language — TypeScript,
    //      JavaScript, CSS, HTML, JSON all use the same workerMain.js;
    //      the language-specific module (`tsWorker.js`, etc.) is loaded
    //      inside the worker via AMD require using the baseUrl above.
    //
    // Why not `getWorkerUrl`?
    //   - `getWorkerUrl` makes Monaco do `new Worker(url)` with no
    //     baseUrl injection. `workerMain.js` then falls back to
    //     `"../../../"` which is unreliable and may produce double
    //     `vs/vs/` paths depending on URL depth.
    //
    // Why not the AMD `require.toUrl` fallback path?
    //   - It relies on `oe.toUrl('vs/base/worker/defaultWorkerFactory.js')`
    //     which requires the *inner* AMD `require` parameter inside
    //     Monaco's compiled module. Hard to reason about from user code.
    //
    // This explicit getWorker approach is the most predictable path.
    // ─────────────────────────────────────────────────────────────────
    function ensureMonacoEnvironment(base) {
        // Always create/replace MonacoEnvironment so that if we retry a
        // different base URL (first CDN failed, trying second), the worker
        // script points to the *current* base rather than the first one.
        if (global.MonacoEnvironment && global.MonacoEnvironment._workerUrl) {
            try { URL.revokeObjectURL(global.MonacoEnvironment._workerUrl); } catch (_) {}
        }
        // Blob workers run in `blob:` origin — `importScripts` requires
        // absolute URLs. Resolve `base` against the page's origin when
        // it's a relative path like `/monaco/min/vs`. If `base` already
        // starts with `http` (CDN URL), leave it as-is.
        const origin = global.location ? global.location.origin : '';
        const absoluteBase = base.startsWith('http') ? base : (origin + base);
        // `baseNoVs` = base with the trailing `/vs` stripped. Worker-side
        // AMD require resolves `'vs/language/typescript/tsWorker'` as
        // `baseNoVs + 'vs/language/typescript/tsWorker.js'` →
        // `https://example.com/monaco/min/vs/language/typescript/tsWorker.js`.
        const baseNoVs = absoluteBase.replace(/\/+vs\/?$/, '/');
        const blobParts = [
            '/* Monaco worker bootstrapper — built by OJ frontend */',
            'globalThis.MonacoEnvironment = { baseUrl: \'' + baseNoVs + '\' };',
            'importScripts(\'' + absoluteBase + '/base/worker/workerMain.js\');',
        ];
        const blob = new Blob(blobParts, { type: 'application/javascript' });
        const workerUrl = URL.createObjectURL(blob);
        global.MonacoEnvironment = {
            _workerUrl: workerUrl,
            getWorker: function (workerId, label) {
                return new Worker(workerUrl, { name: label || 'monaco-worker' });
            },
        };
    }

    const LANGUAGE_MAP = {
        Rust: 'rust',
        Golang: 'go',
        C: 'c',
        'C++': 'cpp',
        Python: 'python',
        Java: 'java',
        JavaScript: 'javascript',
        Ruby: 'ruby',
        Kotlin: 'kotlin',
    };

    // Languages that have no real language service in Monaco — only
    // syntax highlighting from a Monarch tokenizer. For these we make
    // sure validation/semantic tokens are disabled so the user never
    // sees spurious red squiggles.
    const PLAIN_TOKENIZER_LANGUAGES = new Set([
        'rust', 'go', 'c', 'cpp', 'java', 'ruby', 'kotlin',
    ]);

    const STARTERS = {
        Rust: `fn main() {\n    println!(\"Hello, world!\");\n}\n`,
        Golang: `package main\nimport \"fmt\"\n\nfunc main() {\n    fmt.Println(\"Hello, world!\")\n}\n`,
        C: `#include <stdio.h>

int main(void) {
    return 0;
}
`,
        'C++': `#include <iostream>
using namespace std;

int main() {

    return 0;
}
`,
        Python: `# Python 3

def main():
    pass

if __name__ == '__main__':
    main()
`,
        Java: `import java.util.Scanner;

public class Main {
    public static void main(String[] args) {
        Scanner sc = new Scanner(System.in);
    }
}
`,
        JavaScript: `// JavaScript

function main() {
    // Your code here
    console.log("Hello, world!");
}

main();
`,
        Ruby: `# Ruby

def main
    # Your code here
end

main
`,
        Kotlin: `// Kotlin

fun main() {
    // Your code here
}
`,
    };

    // ─────────────────────────────────────────────────────────────────
    // Minimal fallback Monarch tokenizer for Rust — used only if
    // Monaco's own rust contribution is missing on the active CDN.
    // Handles keywords, comments, strings, numbers and macros.
    // ─────────────────────────────────────────────────────────────────
    function registerFallbackRustTokenizer() {
        if (!global.monaco) return;
        const m = global.monaco;
        const existing = m.languages.getLanguages().find((l) => l.id === 'rust');
        if (existing) return; // native contribution available — nothing to do

        m.languages.register({ id: 'rust', extensions: ['.rs'], aliases: ['Rust', 'rust'] });
        m.languages.setMonarchTokensProvider('rust', {
            tokenizer: {
                root: [
                    [/[ \t\r\n]+/, 'white'],
                    [/\/\/\/.*/, 'doc.comment'],
                    [/\/\/.*/, 'comment'],
                    [/\/\*/, 'comment', '@blockComment'],
                    [/"([^"\\]|\\.)*$/, 'string.invalid'],
                    [/"/, 'string', '@string'],
                    [/'[^\\']'/, 'string'],
                    [/'/, 'string'],
                    [/\b(use|let|mut|fn|pub|crate|mod|struct|enum|trait|impl|for|in|if|else|while|loop|match|as|return|break|continue|const|static|type|where|move|ref|Box|Self|self|super|async|await|try|do)\b/, 'keyword'],
                    [/\b(u8|u16|u32|u64|u128|i8|i16|i32|i64|i128|f32|f64|bool|char|str|usize|isize|String|Vec|Option|Result|Box)\b/, 'type'],
                    [/\b(true|false|Some|None|Ok|Err)\b/, 'constant'],
                    [/\b(println|print|format|eprintln|eprint|panic|assert|assert_eq|assert_ne|todo|unreachable|include|include_str|include_bytes|concat|env|cfg|derive|repr|allow|warn|deny|forbid|test|ignore|macro_export)\b/, 'predefined'],
                    [/[a-z_][a-zA-Z0-9_]*\s*[!]/, 'macro'],
                    [/[A-Z][a-zA-Z0-9_]*/, 'type.identifier'],
                    [/[a-z_][a-zA-Z0-9_]*/, 'identifier'],
                    [/\b\d+(?:_?\d)*(?:\.\d+)?(?:[eE][+-]?\d+(?:_?\d)*)?(?:u8|u16|u32|u64|u128|i8|i16|i32|i64|i128|f32|f64|usize|isize)?\b/, 'number'],
                    [/[{}()\[\]]/, '@brackets'],
                    [/[;,.]/, 'delimiter'],
                    [/[<>\-+*/%=&|^!?:]+/, 'operator'],
                ],
                blockComment: [
                    [/[^/*]+/, 'comment'],
                    [/\/\*/, 'comment', '@push'],
                    [/\/\*/, 'comment.invalid'],
                    [/\*\//, 'comment', '@pop'],
                    [/[/*]/, 'comment'],
                ],
                string: [
                    [/[^\\"]+/, 'string'],
                    [/\\./, 'string.escape'],
                    [/"/, 'string', '@pop'],
                ],
            },
        });
        m.languages.setLanguageConfiguration('rust', {
            comments: { lineComment: '//', blockComment: ['/*', '*/'] },
            brackets: [['{', '}'], ['[', ']'], ['(', ')']],
            autoClosingPairs: [
                { open: '{', close: '}' }, { open: '[', close: ']' },
                { open: '(', close: ')' }, { open: '"', close: '"' },
                { open: "'", close: "'" },
            ],
            surroundingPairs: [
                { open: '{', close: '}' }, { open: '[', close: ']' },
                { open: '(', close: ')' }, { open: '"', close: '"' },
                { open: "'", close: "'" },
            ],
            folding: { markers: { start: /^\s*(\{|fn\s+|mod\s+|#\[\w+\]|#!\[[^\]]*\])/, end: /^\s*(\}|;)?\s*$/ } },
        });
    }

    // ─────────────────────────────────────────────────────────────────
    // Configure language services (TypeScript, CSS, HTML, JSON) after
    // Monaco finishes loading. Without explicit compiler/diagnostics
    // options, the workers are loaded but won't emit error markers.
    // ─────────────────────────────────────────────────────────────────
    function configureLanguageServices(monaco) {
        try {
            // ─── JavaScript ─────────────────────────────────────
            monaco.languages.typescript.javascriptDefaults.setCompilerOptions({
                target: monaco.languages.typescript.ScriptTarget.ES2020,
                module: monaco.languages.typescript.ModuleKind.ES2020,
                allowNonTsExtensions: true,
                allowJs: true,
                checkJs: false,
                noEmit: true,
                // Only load ES2020 library — no DOM or Node.js types
                // to avoid false positive errors like "Cannot find
                // name 'process'" or "Cannot find module 'readline'"
                lib: ['ES2020'],
            });
            monaco.languages.typescript.javascriptDefaults.setDiagnosticsOptions({
                noSemanticValidation: false,
                noSyntaxValidation: false,
                noSuggestionDiagnostics: false,
                diagnosticCodesToIgnore: [
                    1108, // "A return statement can only be used within a function body."
                    1378, // "New expression does not satisfy the expected type"
                    2304, // "Cannot find name 'process' / 'require' / 'module'"
                    2580, // "Cannot find name 'require'"
                    2581, // "Cannot find name 'module'"
                    2584, // "Cannot find name 'global'"
                    2686, // "'xxx' refers to a UMD global, but the current file is a module"
                    2792, // "Cannot find module 'xxx'"
                    2307, // "Cannot find module 'xxx'"
                    2339, // "Property 'xxx' does not exist on type"
                    7016, // "Could not find a declaration file for module 'xxx'"
                ],
            });

            // (TypeScript support removed)

            // ─── CSS / SCSS / LESS ─────────────────────────────
            if (monaco.languages.css && monaco.languages.css.cssDefaults) {
                monaco.languages.css.cssDefaults.setOptions({
                    validate: true,
                    lint: {
                        compatibleVendorPrefixes: 'warning',
                        vendorPrefix: 'warning',
                        duplicateProperties: 'warning',
                        emptyRules: 'warning',
                        importStatement: 'ignore',
                    },
                });
            }
            if (monaco.languages.scss && monaco.languages.scss.scssDefaults) {
                monaco.languages.scss.scssDefaults.setOptions({ validate: true });
            }
            if (monaco.languages.less && monaco.languages.less.lessDefaults) {
                monaco.languages.less.lessDefaults.setOptions({ validate: true });
            }

            // ─── HTML ─────────────────────────────────────────
            if (monaco.languages.html && monaco.languages.html.htmlDefaults) {
                monaco.languages.html.htmlDefaults.setOptions({
                    validate: true,
                    format: {
                        tabSize: 2,
                        insertSpaces: true,
                    },
                });
            }

            // ─── JSON ─────────────────────────────────────────
            if (monaco.languages.json && monaco.languages.json.jsonDefaults) {
                monaco.languages.json.jsonDefaults.setDiagnosticsOptions({
                    validate: true,
                    allowComments: true,
                    allowTrailingCommas: true,
                });
            }

            // ─── Trigger worker initialization proactively ───
            // The shared JavaScript/TypeScript worker must spin up before
            // the user types — otherwise syntax errors may not appear in time.
            if (typeof monaco.languages.typescript.getTypeScriptWorker === 'function') {
                monaco.languages.typescript.getTypeScriptWorker().then(function (getWorker) {
                    // Worker is ready — touch it to confirm aliveness.
                    try { getWorker(); } catch (_) {}
                }).catch(function () { /* ignore */ });
            }
        } catch (e) {
            // If anything fails during language-service init, just log it
            // and continue — Monarch tokenizer alone still works fine.
            if (typeof console !== 'undefined' && console.warn) {
                console.warn('[monaco] language service init warning:', e);
            }
        }
    }

    function loadMonaco(onProgress) {
        return new Promise((resolve, reject) => {
            if (global.monaco) {
                resolve(global.monaco);
                return;
            }
            let index = 0;
            const errors = [];
            const tryNext = () => {
                if (index >= MONACO_BASES.length) {
                    reject(new Error(
                        'Monaco editor failed to load from every configured CDN: ' +
                        errors.join('; '),
                    ));
                    return;
                }
                const base = MONACO_BASES[index++];
                if (typeof onProgress === 'function') {
                    try { onProgress('正在加载编辑器（' + base + ')', 'text-muted'); } catch (_) {}
                }
                // IMPORTANT — configure worker path on this base before loading loader.js.
                ensureMonacoEnvironment(base);
                const script = document.createElement('script');
                script.src = `${base}/loader.js`;
                script.onload = () => {
                    try {
                        global.require.config({ paths: { vs: base } });
                        global.require(['vs/editor/editor.main'], () => {
                            try { registerFallbackRustTokenizer(); } catch (_) {}
                            try { configureLanguageServices(global.monaco); } catch (_) {}
                            resolve(global.monaco);
                        }, (err) => {
                            errors.push('require failed for ' + base + ': ' + err);
                            tryNext();
                        });
                    } catch (err) {
                        errors.push('loader.js post-processing failed for ' + base + ': ' + err);
                        tryNext();
                    }
                };
                script.onerror = () => {
                    errors.push('failed to load ' + base + '/loader.js');
                    tryNext();
                };
                document.head.appendChild(script);
            };
            tryNext();
        });
    }

    function draftKey(problemId, language) {
        return `oj_draft_p${problemId}_${language}`;
    }

    function initOJCodeEditor(options) {
        const {
            containerId,
            textareaId,
            languageSelectId,
            problemId,
            initialLanguage = 'C++',
            onProgress,
        } = options;

        const container = document.getElementById(containerId);
        const textarea = document.getElementById(textareaId);
        const languageSelect = document.getElementById(languageSelectId);
        const form = textarea.closest('form');
        const statusEl = document.getElementById('code-editor-status');

        let editor = null;
        let saveTimer = null;

        function setStatus(msg, cls) {
            if (!statusEl) return;
            statusEl.textContent = msg;
            statusEl.className = (cls || 'text-muted');
        }

        function handleProgress(msg, cls) {
            setStatus(msg, cls);
            if (typeof onProgress === 'function') {
                try { onProgress(msg, cls); } catch (_) {}
            }
        }

        function currentLanguage() {
            return languageSelect.value;
        }

        function monacoLang() {
            return LANGUAGE_MAP[currentLanguage()] || 'cpp';
        }

        function loadDraft() {
            try {
                return localStorage.getItem(draftKey(problemId, currentLanguage())) || '';
            } catch {
                return '';
            }
        }

        function saveDraft() {
            if (!editor) return;
            try {
                localStorage.setItem(draftKey(problemId, currentLanguage()), editor.getValue());
                setStatus('草稿已保存');
            } catch {
                setStatus('');
            }
        }

        function setEditorContent(value) {
            const lang = monacoLang();
            editor.setValue(value);
            // setModelLanguage is a STATIC function on monaco.editor,
            // NOT an instance method. Use global.monaco for safety.
            try {
                global.monaco.editor.setModelLanguage(editor.getModel(), lang);
            } catch (_) {
                try { monaco.editor.setModelLanguage(editor.getModel(), lang); } catch (__) {}
            }
            // Clear any stale markers that may have been attached to the
            // previous language model (e.g. leftover TypeScript markers
            // after switching from JS to Rust). This is a purely defensive
            // clean-up; it does NOT disable any features.
            try {
                global.monaco.editor.setModelMarkers(editor.getModel(), 'owner', []);
            } catch (_) {}
            textarea.value = value;
        }

        function applyStarter(force) {
            const starter = STARTERS[currentLanguage()] || '';
            const draft = loadDraft();
            const value = force ? starter : (draft || editor.getValue() || starter);
            setEditorContent(value);
        }

        function switchLanguage() {
            const oldLang = languageSelect.dataset.prevLang;
            if (oldLang) {
                try {
                    localStorage.setItem(draftKey(problemId, oldLang), editor.getValue());
                } catch { /* ignore */ }
            }
            languageSelect.dataset.prevLang = currentLanguage();
            const draft = loadDraft();
            const value = draft || STARTERS[currentLanguage()] || '';
            setEditorContent(value);
        }

        return loadMonaco(handleProgress).then((monaco) => {
            const lang = LANGUAGE_MAP[initialLanguage] || 'cpp';
            const initial = loadDraft() || STARTERS[initialLanguage] || '';

            const editorOptions = {
                value: initial,
                language: lang,
                theme: 'vs-dark',
                automaticLayout: true,
                fontSize: 14,
                tabSize: 4,
                insertSpaces: true,
                minimap: { enabled: true },
                scrollBeyondLastLine: false,
                wordWrap: 'off',
                lineNumbers: 'on',
                renderWhitespace: 'selection',
                bracketPairColorization: { enabled: true },
                guides: { bracketPairs: true },
                suggestOnTriggerCharacters: true,
                quickSuggestions: true,
                folding: true,
                formatOnPaste: false,
                autoClosingBrackets: 'always',
                autoClosingQuotes: 'always',
                autoIndent: 'full',
            };

            editor = monaco.editor.create(container, editorOptions);

            // Clear any markers a worker may have pushed before the
            // correct language was attached (e.g. stale TypeScript markers
            // from a previous session in the same worker).
            try {
                monaco.editor.setModelMarkers(editor.getModel(), 'owner', []);
            } catch (_) {}

            textarea.value = initial;

            editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
                saveDraft();
            });

            editor.onKeyDown((e) => {
                if (e.keyCode === monaco.KeyCode.Tab && !e.ctrlKey && !e.metaKey && !e.altKey) {
                    e.preventDefault();
                    e.stopPropagation();
                    editor.trigger('keyboard', 'tab', null);
                }
            });

            editor.onDidChangeModelContent(() => {
                textarea.value = editor.getValue();
                clearTimeout(saveTimer);
                saveTimer = setTimeout(saveDraft, 800);
            });

            languageSelect.addEventListener('change', switchLanguage);
            languageSelect.dataset.prevLang = currentLanguage();

            var btnReset = document.getElementById('btn-reset-template');
            if (btnReset) btnReset.addEventListener('click', () => {
                if (confirm('确定用该语言模板替换当前代码？')) {
                    applyStarter(true);
                }
            });

            var btnFormat = document.getElementById('btn-format-code');
            if (btnFormat) btnFormat.addEventListener('click', () => {
                var action = editor.getAction('editor.action.formatDocument');
                if (action) action.run();
            });

            form.addEventListener('submit', () => {
                textarea.value = editor.getValue();
            });

            handleProgress('已就绪', 'text-success');
            return editor;
        });
    }

    global.initOJCodeEditor = initOJCodeEditor;
})(window);
