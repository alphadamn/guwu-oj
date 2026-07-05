/**
 * Monaco Editor wrapper for OJ submit page.
 * https://github.com/microsoft/monaco-editor
 *
 * The primary source is configurable in the admin panel (SiteConfig).
 * If the configured source fails to load, the loader falls back
 * through a list of well-known mirrors before giving up.
 */
(function (global) {
    const MONACO_VERSION = '0.52.2';

    // List of fallback mirrors. The FIRST entry is the one configured
    // by the server via ``window.OJ_MONACO_BASE``; remaining entries
    // act as fallbacks if the primary source fails on load.
    const MONACO_BASE_CANDIDATES = [
        (global.OJ_MONACO_BASE || '').toString().trim() ||
            `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`,
        `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`,
        `https://unpkg.com/monaco-editor@${MONACO_VERSION}/min/vs`,
        `https://cdn.bootcdn.net/ajax/libs/monaco-editor/${MONACO_VERSION}/min/vs`,
        `https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/${MONACO_VERSION}/min/vs`,
    ];
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

    const LANGUAGE_MAP = {
        Rust: 'rust',
        Golang: 'go',
        C: 'c',
        'C++': 'cpp',
        Python: 'python',
        Java: 'java',
        JavaScript: 'javascript',
        TypeScript: 'typescript',
        Ruby: 'ruby',
        Kotlin: 'kotlin',
    };

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
        JavaScript: `// Node.js

const readline = require('readline');

const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout
});

let input = [];
rl.on('line', (line) => {
    input.push(line);
}).on('close', () => {
    // Your code here
});
`,
        TypeScript: `// TypeScript

const readline = require('readline');

const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout
});

let input: string[] = [];
rl.on('line', (line: string) => {
    input.push(line);
}).on('close', () => {
    // Your code here
});
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
                    try {
                        onProgress('正在加载编辑器（' + base + ')', 'text-muted');
                    } catch (_) {}
                }
                const script = document.createElement('script');
                script.src = `${base}/loader.js`;
                script.onload = () => {
                    try {
                        global.require.config({ paths: { vs: base } });
                        global.require(['vs/editor/editor.main'], () => {
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
            editor.setValue(value);
            monaco.editor.setModelLanguage(editor.getModel(), monacoLang());
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

            editor = monaco.editor.create(container, {
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
                formatOnPaste: true,
                autoClosingBrackets: 'always',
                autoClosingQuotes: 'always',
                autoIndent: 'full',
            });

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

            document.getElementById('btn-reset-template')?.addEventListener('click', () => {
                if (confirm('确定用该语言模板替换当前代码？')) {
                    applyStarter(true);
                }
            });

            document.getElementById('btn-format-code')?.addEventListener('click', () => {
                editor.getAction('editor.action.formatDocument')?.run();
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
