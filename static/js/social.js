document.addEventListener('DOMContentLoaded', async () => {
    const t = (key, fallback) => window.PemuI18n?.t(key, fallback) || fallback || key;
    try {
        const response = await fetch('/api/public-config', { credentials: 'same-origin' });
        if (!response.ok) throw new Error('No se pudieron cargar los enlaces sociales.');
        const config = await response.json();
        const links = config.social_links || {};
        let visibleProfiles = 0;
        document.querySelectorAll('[data-social-link]').forEach((anchor) => {
            const href = links[anchor.dataset.socialLink];
            if (!href) {
                anchor.remove();
                return;
            }
            anchor.href = href;
            visibleProfiles += 1;
        });
        const section = document.querySelector('[data-social-links]');
        if (section) section.hidden = visibleProfiles === 0;
        document.querySelectorAll('[data-whatsapp-link]').forEach((anchor) => {
            if (!links.whatsapp) {
                anchor.remove();
                return;
            }
            anchor.href = links.whatsapp;
            anchor.hidden = false;
        });
    } catch (error) {
        console.warn(error.message);
    }

    const shareTargets = {
        whatsapp: (url, title, image) => `https://wa.me/?text=${encodeURIComponent(`${title} ${url}`)}`,
        facebook: (url) => `https://www.facebook.com/sharer/sharer.php?u=${encodeURIComponent(url)}`,
        pinterest: (url, title, image) => `https://www.pinterest.com/pin/create/button/?url=${encodeURIComponent(url)}&media=${encodeURIComponent(image)}&description=${encodeURIComponent(title)}`,
    };
    document.querySelectorAll('[data-product-share]').forEach((anchor) => {
        anchor.addEventListener('click', (event) => {
            const target = shareTargets[anchor.dataset.productShare];
            if (!target) return;
            const url = window.location.href;
            const title = document.querySelector('#detail-name')?.textContent || document.title;
            const image = document.querySelector('#detail-image')?.src || '';
            anchor.href = target(url, title, image);
            window.trackStoreEvent?.('share', { method: anchor.dataset.productShare });
        }, { capture: true });
    });

    const copyButton = document.getElementById('copy-product-link');
    const shareMessage = document.getElementById('product-share-message');
    if (copyButton && shareMessage) {
        copyButton.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(window.location.href);
                shareMessage.textContent = t('copiedProductLink', 'Enlace copiado.');
                window.trackStoreEvent?.('share', { method: 'copy_link' });
            } catch (error) {
                shareMessage.textContent = t('copyProductLinkFallback', 'No se pudo copiar. Copia el enlace desde la barra del navegador.');
                console.warn('No se pudo copiar el enlace del producto:', error);
            }
        });
    }
});
