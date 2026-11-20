document.addEventListener('DOMContentLoaded', () => {
    const menu = document.getElementById('account-menu');
    const toggle = document.getElementById('account-menu-toggle');
    const dropdown = document.getElementById('account-dropdown');
    const label = document.getElementById('nav-account-label');
    const logoutButton = document.querySelector('[data-auth-logout]');
    if (!menu || !toggle || !dropdown || !label || !logoutButton) return;

    let csrfToken = '';
    let currentUser = null;
    const t = (key, fallback) => window.PemuI18n?.t(key, fallback) || fallback;

    function renderUser(user) {
        currentUser = user;
        label.textContent = user ? user.name.split(' ')[0] : t('login', 'Iniciar sesión');
        logoutButton.hidden = !user;
        toggle.setAttribute('aria-label', user ? user.name : t('login', 'Iniciar sesión'));
        window.dispatchEvent(new CustomEvent('pemu-auth-change', { detail: { user } }));
    }

    toggle.addEventListener('click', () => {
        const open = toggle.getAttribute('aria-expanded') !== 'true';
        dropdown.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
    });

    document.addEventListener('click', (event) => {
        if (menu.contains(event.target)) return;
        dropdown.hidden = true;
        toggle.setAttribute('aria-expanded', 'false');
    });

    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        dropdown.hidden = true;
        toggle.setAttribute('aria-expanded', 'false');
    });

    logoutButton.addEventListener('click', async () => {
        logoutButton.disabled = true;
        try {
            const response = await fetch('/api/auth/logout', {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrfToken,
                },
                body: '{}',
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'No se pudo cerrar la sesión.');
            renderUser(null);
            window.location.reload();
        } catch (error) {
            console.error('No se pudo cerrar la sesión desde el encabezado:', error);
            logoutButton.disabled = false;
        }
    });

    document.addEventListener('pemu-language-change', () => {
        if (!currentUser) label.textContent = t('login', 'Iniciar sesión');
    });

    fetch('/api/auth/me', { credentials: 'same-origin', cache: 'no-store' })
        .then(async (response) => {
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'No se pudo consultar la sesión.');
            csrfToken = result.csrf_token || '';
            renderUser(result.user);
        })
        .catch((error) => {
            console.error('No se pudo consultar la sesión del encabezado:', error);
        });
});
