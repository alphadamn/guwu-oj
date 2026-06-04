/**
 * Monaco Editor wrapper for OJ submit page.
 * https://github.com/microsoft/monaco-editor
 */
(function (global) {
    const MONACO_VERSION = '0.52.2';
    const MONACO_BASE = `https://cdn.jsdelivr.net/npm/monaco-editor@${MONACO_VERSION}/min/vs`;

    const LANGUAGE_MAP = {
        C: 'c',
        'C++': 'cpp',
        Python: 'python',
        Java: 'java',
        Assembly: 'asm',
    };

    const STARTERS = {
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
        Assembly: `.section .text
.global _start

_start:
    ; 退出程序 (exit system call)
    mov x0, #0       ; exit code 0
    mov x8, #93      ; sys_exit on ARM64
    svc #0          ; system call
`,
    };

    function loadMonaco() {
        return new Promise((resolve, reject) => {
            if (global.monaco) {
                resolve(global.monaco);
                return;
            }
            const script = document.createElement('script');
            script.src = `${MONACO_BASE}/loader.js`;
            script.onload = () => {
                global.require.config({ paths: { vs: MONACO_BASE } });
                global.require(['vs/editor/editor.main'], () => {
                    resolve(global.monaco);
                }, reject);
            };
            script.onerror = () => reject(new Error('Failed to load Monaco Editor'));
            document.head.appendChild(script);
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
        } = options;

        const container = document.getElementById(containerId);
        const textarea = document.getElementById(textareaId);
        const languageSelect = document.getElementById(languageSelectId);
        const form = textarea.closest('form');
        const statusEl = document.getElementById('code-editor-status');

        let editor = null;
        let saveTimer = null;

        function setStatus(msg) {
            if (statusEl) {
                statusEl.textContent = msg;
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

        return loadMonaco().then((monaco) => {
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

            return editor;
        });
    }

    global.initOJCodeEditor = initOJCodeEditor;
})(window);
