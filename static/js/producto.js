document.addEventListener('DOMContentLoaded', async () => {
    let csrfToken = '';
    let currentUser = null;
    let product = null;
    let cart = { items: [], count: 0, total: 0 };
    let maxCanAdd = 0;
    let saleTimerInterval = null;
    let saleRefreshPending = false;
    const byId = (id) => document.getElementById(id);
    let currency = new Intl.NumberFormat(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO', {
        style: 'currency',
        currency: 'COP',
        currencyDisplay: 'code',
        maximumFractionDigits: 0,
    });
    const t = (key, fallback) => window.PemuI18n.t(key, fallback);
    const productId = decodeURIComponent(window.location.pathname.split('/').filter(Boolean).pop() || '');

    async function api(path, options = {}) {
        const method = (options.method || 'GET').toUpperCase();
        const headers = { ...(options.headers || {}) };
        if (options.body) headers['Content-Type'] = 'application/json';
        if (method !== 'GET' && !csrfToken) await refreshCsrfToken();
        if (method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
        const requestOptions = { ...options, method, headers, credentials: 'same-origin' };
        let response = await fetch(path, requestOptions);
        if (method !== 'GET' && response.status === 403) {
            await refreshCsrfToken();
            headers['X-CSRF-Token'] = csrfToken;
            response = await fetch(path, requestOptions);
        }
        const data = await response.json();
        if (!response.ok) {
            const error = new Error(data.error || 'No se pudo completar la solicitud.');
            error.status = response.status;
            throw error;
        }
        return data;
    }

    async function refreshCsrfToken() {
        const response = await fetch('/api/session', { credentials: 'same-origin', cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || !data.csrf_token) throw new Error('No se pudo validar la sesión. Recarga la página e inténtalo de nuevo.');
        csrfToken = data.csrf_token;
    }

    function imagePath(path) {
        return /^https:\/\//i.test(path) ? path : `/${path.split('/').map(encodeURIComponent).join('/')}`;
    }

    document.addEventListener('error', (event) => {
        const image = event.target;
        if (image instanceof HTMLImageElement && /^https:\/\//i.test(image.src) && !image.dataset.fallback) {
            image.dataset.fallback = 'true';
            image.src = '/imagenes/img1.jpg';
        }
    }, true);
    function showUnavailable(message) {
        byId('detail-loading').hidden = true;
        byId('detail-unavailable').hidden = false;
        byId('detail-unavailable').querySelector('p').textContent = message;
    }

    function updateQuantityControls() {
        const input = byId('detail-quantity');
        const quantity = Number(input.value) || 1;
        input.value = String(Math.max(1, Math.min(maxCanAdd || 1, quantity)));
        input.max = String(Math.max(1, maxCanAdd));
        byId('quantity-minus').disabled = Number(input.value) <= 1;
        byId('quantity-plus').disabled = maxCanAdd === 0 || Number(input.value) >= maxCanAdd;
        byId('detail-add').disabled = maxCanAdd === 0;
    }

    function updateSaleCountdown() {
        const timer = byId('detail-sale-timer');
        if (saleTimerInterval) {
            window.clearInterval(saleTimerInterval);
            saleTimerInterval = null;
        }
        timer.hidden = !product.flash_sale_ends_at;
        if (timer.hidden) return;

        const update = () => {
            const remaining = new Date(product.flash_sale_ends_at).getTime() - Date.now();
            if (remaining <= 0) {
                window.clearInterval(saleTimerInterval);
                saleTimerInterval = null;
                if (saleRefreshPending) return;
                saleRefreshPending = true;
                api(`/api/products/${encodeURIComponent(productId)}`)
                    .then((data) => {
                        product = data.product;
                        renderProduct();
                    })
                    .catch((error) => {
                        timer.textContent = `La oferta terminó. No se pudo actualizar el precio: ${error.message}`;
                    })
                    .finally(() => {
                        saleRefreshPending = false;
                    });
                return;
            }
            const seconds = Math.floor(remaining / 1000);
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            const rest = seconds % 60;
            timer.textContent = `${window.PemuI18n.language === 'en' ? 'Flash sale ends in' : 'Oferta relámpago: termina en'} ${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`;
        };

        update();
        if (!saleRefreshPending) saleTimerInterval = window.setInterval(update, 1000);
    }

    function renderProduct() {
        const existing = currentUser ? cart.items.find((item) => item.id === product.id) : null;
        maxCanAdd = Math.max(0, Math.min(20 - (existing?.quantity || 0), product.stock - (existing?.quantity || 0)));
        byId('detail-loading').hidden = true;
        byId('product-detail').hidden = false;
        document.title = `${product.name} | XrteShop`;
        byId('detail-image').src = imagePath(product.image);
        byId('detail-image').alt = product.name;
        byId('detail-badge').textContent = product.badge;
        byId('detail-category').textContent = window.PemuI18n.category(product.category);
        byId('detail-fact-category').textContent = window.PemuI18n.category(product.category);
        byId('detail-name').textContent = product.name;
        byId('detail-seller').textContent = `${t('soldBy')} ${product.seller_name || 'XrteShop'}`;
        byId('detail-fact-seller').textContent = product.seller_name || 'XrteShop';
        byId('detail-description').textContent = product.description;
        byId('detail-price').textContent = currency.format(product.price);
        byId('detail-original-price').textContent = currency.format(product.original_price);
        byId('detail-original-price').hidden = product.original_price <= product.price;
        updateSaleCountdown();
        byId('detail-stock-text').textContent = product.stock > 0 ? `${product.stock} ${t('availableUnits')}` : t('soldOut');
        byId('detail-stock').classList.toggle('sold-out', product.stock === 0);
        byId('detail-purchase').hidden = maxCanAdd === 0;
        if (maxCanAdd === 0) {
            byId('detail-message').textContent = product.stock === 0 ? t('itemSoldOut') : t('bagMaximum');
        }
        const source = byId('detail-source');
        if (product.source_url) {
            source.href = product.source_url;
            source.hidden = false;
        }
        byId('detail-cart-count').textContent = String(cart.count);
        updateQuantityControls();
    }

    byId('quantity-minus').addEventListener('click', () => {
        byId('detail-quantity').value = String(Number(byId('detail-quantity').value) - 1);
        updateQuantityControls();
    });
    byId('quantity-plus').addEventListener('click', () => {
        byId('detail-quantity').value = String(Number(byId('detail-quantity').value) + 1);
        updateQuantityControls();
    });
    byId('detail-quantity').addEventListener('input', updateQuantityControls);
    byId('detail-quantity').addEventListener('change', updateQuantityControls);

    function redirectToSignIn(quantity) {
        const returnPath = `${window.location.pathname}?add=${encodeURIComponent(quantity)}`;
        window.location.assign(`/cuenta?next=${encodeURIComponent(returnPath)}`);
    }

    async function addToCart(quantityToAdd) {
        if (!currentUser) {
            redirectToSignIn(quantityToAdd);
            return;
        }
        const button = byId('detail-add');
        const message = byId('detail-message');
        const existing = cart.items.find((item) => item.id === product.id);
        const quantity = (existing?.quantity || 0) + quantityToAdd;
        button.disabled = true;
        message.classList.remove('error');
        message.textContent = '';
        try {
            cart = await api('/api/cart', {
                method: 'POST',
                body: JSON.stringify({ product_id: product.id, quantity }),
            });
            byId('detail-cart-count').textContent = String(cart.count);
            message.textContent = `${product.name} ${t('addedBag').toLowerCase()}.`;
            renderProduct();
            message.textContent = `${product.name} ${t('addedBag').toLowerCase()}.`;
        } catch (error) {
            if (error.status === 401) {
                currentUser = null;
                redirectToSignIn(quantityToAdd);
                return;
            }
            message.classList.add('error');
            message.textContent = error.message;
            button.disabled = maxCanAdd === 0;
        }
    }

    byId('detail-add').addEventListener('click', () => addToCart(Number(byId('detail-quantity').value)));
    document.addEventListener('pemu-language-change', () => {
        currency = new Intl.NumberFormat(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO', {
            style: 'currency',
            currency: 'COP',
            currencyDisplay: 'code',
            maximumFractionDigits: 0,
        });
        if (product) renderProduct();
    });

    try {
        const sessionData = await api('/api/session');
        csrfToken = sessionData.csrf_token;
        currentUser = sessionData.user;
        const [cartData, productData] = await Promise.all([
            api('/api/cart'),
            api(`/api/products/${encodeURIComponent(productId)}`),
        ]);
        cart = cartData;
        product = productData.product;
        renderProduct();
        const pendingQuantity = Number(new URLSearchParams(window.location.search).get('add'));
        if (currentUser && Number.isInteger(pendingQuantity) && pendingQuantity > 0 && pendingQuantity <= 20) {
            window.history.replaceState(null, '', window.location.pathname);
            await addToCart(pendingQuantity);
        }
    } catch (error) {
        showUnavailable(error.message.includes('404') ? 'Este producto ya no aparece en el catálogo.' : error.message);
    }
});