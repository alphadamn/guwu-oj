// Extracted from templates/admin/devlog/env_generator.html inline <script> (2026-09-13).
document.addEventListener('DOMContentLoaded', () => {
    const $ = (id) => document.getElementById(id);
    const value = (id) => $(id).value.trim();
    const machines = $('machines');

    function quote(value) {
        if (!value) return '';
        return /[\s#"'\\]/.test(value) ? JSON.stringify(value) : value;
    }

    function addMachine(machine = {}) {
        const node = $('machine-template').content.firstElementChild.cloneNode(true);
        const index = machines.children.length + 1;
        node.querySelector('.machine-title').textContent = `评测机 ${index}`;
        node.querySelector('.machine-name').value = machine.name || `judge-${index}`;
        node.querySelector('.machine-host').value = machine.host || '127.0.0.1';
        node.querySelector('.machine-port').value = machine.port || '6379';
        node.querySelector('.machine-db').value = machine.db || '0';
        node.querySelector('.machine-queue').value = machine.queue || `judge-${index}`;
        node.querySelector('.machine-weight').value = machine.weight || '1';
        node.querySelector('.machine-tls').value = machine.tls ? 'true' : 'false';
        node.querySelector('.machine-ca').value = machine.ca_cert_path || '';
        node.querySelector('.machine-client-cert').value = machine.client_cert_path || '';
        node.querySelector('.machine-client-key').value = machine.client_key_path || '';
        node.querySelector('.machine-password').value = machine.password || '';
        node.querySelector('.remove-machine').addEventListener('click', () => { node.remove(); renumberMachines(); });
        machines.appendChild(node);
    }

    function renumberMachines() {
        [...machines.children].forEach((node, index) => {
            node.querySelector('.machine-title').textContent = `评测机 ${index + 1}`;
        });
    }

    function secret() {
        const bytes = new Uint8Array(48);
        crypto.getRandomValues(bytes);
        return [...bytes].map((byte) => 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*-_'.charAt(byte % 72)).join('');
    }

    function generate() {
        const djangoSecret = value('django-secret') || secret();
        $('django-secret').value = djangoSecret;
        const lines = [
            '# Generated locally by Guwu OJ Admin. Do not commit this file.',
            `DJANGO_SECRET_KEY=${quote(djangoSecret)}`,
            `DJANGO_DEBUG=${value('django-debug')}`,
            `DJANGO_ALLOWED_HOSTS=${quote(value('django-hosts'))}`,
            '', '# PostgreSQL',
            `DB_NAME=${quote(value('db-name'))}`,
            `DB_USER=${quote(value('db-user'))}`,
            `DB_PASSWORD=${quote(value('db-password'))}`,
            `DB_HOST=${quote(value('db-host'))}`,
            `DB_PORT=${quote(value('db-port'))}`,
            `DB_SSLMODE=${value('db-sslmode')}`,
            ...(value('db-sslrootcert') ? [`DB_SSLROOTCERT=${quote(value('db-sslrootcert'))}`] : []),
            '', '# Cache Redis',
            `CACHE_REDIS_HOST=${quote(value('cache-host'))}`,
            `CACHE_REDIS_PORT=${quote(value('cache-port'))}`,
            `CACHE_REDIS_DB=${quote(value('cache-db'))}`,
            `CACHE_REDIS_PASSWORD=${quote(value('cache-password'))}`,
            `CACHE_REDIS_TLS=${value('cache-tls')}`,
            '', '# RQ Redis',
            `RQ_REDIS_HOST=${quote(value('rq-host'))}`,
            `RQ_REDIS_PORT=${quote(value('rq-port'))}`,
            `RQ_REDIS_DB=${quote(value('rq-db'))}`,
            `RQ_REDIS_PASSWORD=${quote(value('rq-password'))}`,
            `RQ_REDIS_TLS=${value('rq-tls')}`,
        ];
        if (value('cache-tls') === 'true') lines.push(`CACHE_REDIS_CA_CERT=${quote(value('cache-ca'))}`);
        if (value('rq-tls') === 'true') lines.push(`RQ_REDIS_CA_CERT=${quote(value('rq-ca'))}`);
        lines.push('', '# OJ runtime', `OJ_MULTI_JUDGE_ENABLED=${value('multi-judge')}`, `OJ_ROLE=${value('oj-role')}`, `OJ_DOCKER_ENABLED=${value('docker-enabled')}`, `OJ_DOCKER_IMAGE=${quote(value('docker-image'))}`, `OJ_DOCKER_PIDS_LIMIT=${quote(value('docker-pids'))}`, `OJ_JUDGE_CONCURRENCY=${quote(value('judge-concurrency'))}`, `OJ_SUBPROCESS_TIMEOUT_SEC=${quote(value('subprocess-timeout'))}`);
        const machineConfig = [...machines.children].map((machine, index) => ({
            name: machine.querySelector('.machine-name').value.trim() || `judge-${index + 1}`,
            host: machine.querySelector('.machine-host').value.trim() || '127.0.0.1',
            port: Number(machine.querySelector('.machine-port').value.trim() || 6379),
            db: Number(machine.querySelector('.machine-db').value.trim() || 0),
            queue: machine.querySelector('.machine-queue').value.trim() || `judge-${index + 1}`,
            enabled: true,
            weight: Number(machine.querySelector('.machine-weight').value.trim() || 1),
            tls: machine.querySelector('.machine-tls').value === 'true',
            ca_cert_path: machine.querySelector('.machine-ca').value.trim(),
            client_cert_path: machine.querySelector('.machine-client-cert').value.trim(),
            client_key_path: machine.querySelector('.machine-client-key').value.trim(),
            password: machine.querySelector('.machine-password').value,
        }));
        lines.push('', '# Judge machines', `JUDGE_MACHINES_JSON=${quote(JSON.stringify(machineConfig))}`);
        lines.push('', '# SMTP', `EMAIL_BACKEND=${quote(value('email-backend'))}`, `EMAIL_HOST=${quote(value('email-host'))}`, `EMAIL_PORT=${quote(value('email-port'))}`, `EMAIL_HOST_USER=${quote(value('email-user'))}`, `EMAIL_HOST_PASSWORD=${quote(value('email-password'))}`, `EMAIL_USE_TLS=${value('email-tls')}`, `EMAIL_USE_SSL=${value('email-ssl')}`, `DEFAULT_FROM_EMAIL=${quote(value('from-email'))}`, `SERVER_EMAIL=${quote(value('server-email'))}`, `ADMINS_CSV=${quote(value('admins-csv'))}`, '');
        $('output').value = lines.join('\n');
    }

    $('add-machine').addEventListener('click', () => addMachine());
    $('generate').addEventListener('click', generate);
    $('copy').addEventListener('click', async () => { generate(); await navigator.clipboard.writeText($('output').value); $('copy').textContent = '已复制'; setTimeout(() => { $('copy').textContent = '复制内容'; }, 1500); });
    $('download').addEventListener('click', () => { generate(); const blob = new Blob([$('output').value], { type: 'text/plain;charset=utf-8' }); const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = '.env'; link.click(); URL.revokeObjectURL(link.href); });
    addMachine();
    generate();
});
