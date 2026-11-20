document.addEventListener('DOMContentLoaded', async () => {
    const storageKey = 'xrteshop-privacy-consent';
    const english = window.PemuI18n?.language === 'en';
    const savedConsent = () => {
        try {
            const value = JSON.parse(localStorage.getItem(storageKey) || 'null');
            return value && value.necessary === true ? value : null;
        } catch (error) {
            console.warn('No se pudieron leer las preferencias de privacidad:', error);
            return null;
        }
    };

    let consent = savedConsent();
    let config = {};
    let analyticsLoaded = false;
    let marketingLoaded = false;

    function loadAnalytics() {
        if (analyticsLoaded || !/^G-[A-Z0-9]+$/i.test(config.ga4_id || '')) return;
        analyticsLoaded = true;
        window.dataLayer = window.dataLayer || [];
        window.gtag = function gtag() { window.dataLayer.push(arguments); };
        window.gtag('js', new Date());
        window.gtag('config', config.ga4_id, { anonymize_ip: true });
        const script = document.createElement('script');
        script.async = true;
        script.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(config.ga4_id)}`;
        document.head.append(script);
    }

    function loadMarketing() {
        if (marketingLoaded || !/^\d{5,20}$/.test(config.meta_pixel_id || '')) return;
        marketingLoaded = true;
        (function (f, b, e, v, n, t, s) {
            if (f.fbq) return;
            n = f.fbq = function () {
                n.callMethod ? n.callMethod.apply(n, arguments) : n.queue.push(arguments);
            };
            if (!f._fbq) f._fbq = n;
            n.push = n;
            n.loaded = true;
            n.version = '2.0';
            n.queue = [];
            t = b.createElement(e);
            t.async = true;
            t.src = v;
            s = b.getElementsByTagName(e)[0];
            s.parentNode.insertBefore(t, s);
        })(window, document, 'script', 'https://connect.facebook.net/en_US/fbevents.js');
        window.fbq('init', config.meta_pixel_id);
        window.fbq('track', 'PageView');
    }

    function applyConsent() {
        if (consent?.analytics) loadAnalytics();
        if (consent?.marketing) loadMarketing();
    }

    window.trackStoreEvent = (name, parameters = {}) => {
        if (consent?.analytics && typeof window.gtag === 'function') {
            window.gtag('event', name, parameters);
        }
        if (consent?.marketing && typeof window.fbq === 'function') {
            window.fbq('trackCustom', name, parameters);
        }
    };

    try {
        const response = await fetch('/api/public-config', { credentials: 'same-origin' });
        if (!response.ok) throw new Error('No se pudieron cargar las opciones de privacidad.');
        config = await response.json();
    } catch (error) {
        console.warn(error.message);
    }
    applyConsent();

    const banner = document.createElement('section');
    banner.className = 'privacy-banner';
    banner.hidden = Boolean(consent);
    banner.setAttribute('aria-label', 'Preferencias de cookies');
    banner.innerHTML = `
        <div><strong>${english ? 'Your privacy matters' : 'Tu privacidad importa'}</strong><p>${english ? 'We use necessary storage to run the shop. Analytics and marketing are enabled only with your permission. You can change your choice at any time.' : 'Usamos cookies necesarias para que la tienda funcione. Analítica y marketing solo se activan si das permiso. Puedes cambiar tu elección cuando quieras.'}</p></div>
        <div class="privacy-banner-actions">
            <button type="button" data-choice="necessary">${english ? 'Necessary only' : 'Solo necesarias'}</button>
            <button type="button" data-choice="all">${english ? 'Accept all' : 'Aceptar todas'}</button>
            <button type="button" data-open-settings>${english ? 'Configure' : 'Configurar'}</button>
        </div>`;
    document.body.append(banner);

    const settings = document.createElement('dialog');
    settings.className = 'privacy-dialog';
    settings.setAttribute('aria-labelledby', 'privacy-dialog-title');
    settings.innerHTML = `
        <form method="dialog">
            <h2 id="privacy-dialog-title">${english ? 'Privacy preferences' : 'Preferencias de privacidad'}</h2>
            <p>${english ? 'Necessary storage keeps your session and bag working. It cannot be disabled.' : 'Las cookies necesarias mantienen activa la sesión y la bolsa. No se pueden desactivar.'}</p>
            <label><span>${english ? 'Necessary cookies' : 'Cookies necesarias'} <small>${english ? 'Always active' : 'Siempre activas'}</small></span><input type="checkbox" checked disabled></label>
            <label><span>${english ? 'Analytics' : 'Analítica'} <small>${english ? 'Helps us understand shop usage' : 'Ayuda a entender el uso de la tienda'}</small></span><input name="analytics" type="checkbox"></label>
            <label><span>${english ? 'Marketing' : 'Marketing'} <small>${english ? 'Allows advertising pixels' : 'Permite píxeles de publicidad'}</small></span><input name="marketing" type="checkbox"></label>
            <div class="privacy-dialog-actions">
                <button type="button" data-save>${english ? 'Save preferences' : 'Guardar preferencias'}</button>
                <button type="submit">${english ? 'Close' : 'Cerrar'}</button>
            </div>
        </form>`;
    document.body.append(settings);

    function savePreferences(nextConsent) {
        const previouslyAllowedOptional = Boolean(consent?.analytics || consent?.marketing);
        consent = {
            necessary: true,
            analytics: Boolean(nextConsent.analytics),
            marketing: Boolean(nextConsent.marketing),
            updated_at: new Date().toISOString(),
        };
        try {
            localStorage.setItem(storageKey, JSON.stringify(consent));
        } catch (error) {
            console.error('No se pudieron guardar las preferencias de privacidad:', error);
            return;
        }
        banner.hidden = true;
        applyConsent();
        if (previouslyAllowedOptional && !consent.analytics && !consent.marketing) {
            window.location.reload();
        }
    }

    function openSettings() {
        const current = consent || {};
        settings.querySelector('[name="analytics"]').checked = Boolean(current.analytics);
        settings.querySelector('[name="marketing"]').checked = Boolean(current.marketing);
        settings.showModal();
    }

    banner.addEventListener('click', (event) => {
        if (event.target.closest('[data-open-settings]')) {
            openSettings();
            return;
        }
        const choice = event.target.closest('[data-choice]')?.dataset.choice;
        if (choice) savePreferences({ analytics: choice === 'all', marketing: choice === 'all' });
    });
    settings.querySelector('[data-save]').addEventListener('click', () => {
        savePreferences({
            analytics: settings.querySelector('[name="analytics"]').checked,
            marketing: settings.querySelector('[name="marketing"]').checked,
        });
        settings.close();
    });
    document.querySelectorAll('[data-cookie-settings]').forEach((button) => {
        button.addEventListener('click', openSettings);
    });
});
