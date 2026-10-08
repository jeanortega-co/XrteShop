document.addEventListener('DOMContentLoaded', () => {
    let csrfToken = '';
    let currentUser = null;
    let cart = { items: [], count: 0, total: 0 };
    let products = [];
    let toastTimer;
    let countdownTimer;
    let searchTimer;
    let catalogRequestId = 0;
    let appliedCoupon = null;
    let currentPage = 1;
    let totalPages = 1;
    const pageSize = 12;
    let currency = new Intl.NumberFormat(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO', {
        style: 'currency',
        currency: 'COP',
        currencyDisplay: 'code',
        maximumFractionDigits: 0,
    });

    const byId = (id) => document.getElementById(id);
    const grid = byId('product-grid');
    const drawer = byId('cart-drawer');
    const scrim = byId('drawer-scrim');
    const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
    const t = (key, fallback) => window.PemuI18n.t(key, fallback);

    document.addEventListener('error', (event) => {
        const image = event.target;
        if (image instanceof HTMLImageElement && /^https:\/\//i.test(image.src) && !image.dataset.fallback) {
            image.dataset.fallback = 'true';
            image.src = '/imagenes/img1.jpg';
        }
    }, true);

    async function refreshCsrfToken() {
        const response = await fetch('/api/session', { credentials: 'same-origin', cache: 'no-store' });
        const data = await response.json();
        if (!response.ok || !data.csrf_token) throw new Error('No se pudo validar la sesión. Recarga la página e inténtalo de nuevo.');
        csrfToken = data.csrf_token;
    }

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

    function showToast(message) {
        const toast = byId('toast');
        toast.textContent = message;
        toast.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toast.classList.remove('show'), 2500);
    }

    function imagePath(path) {
        if (/^https:\/\//i.test(path)) return path;
        return `/${path.split('/').map(encodeURIComponent).join('/')}`;
    }

    function renderSkeletons() {
        grid.hidden = false;
        grid.setAttribute('aria-busy', 'true');
        grid.innerHTML = Array.from({ length: 8 }, () => `
            <div class="skeleton-card" aria-hidden="true">
                <span class="skeleton skeleton-image"></span>
                <span class="skeleton skeleton-line"></span>
                <span class="skeleton skeleton-line short"></span>
            </div>`).join('');
    }

    function productPrice(_product, amount) {
        return currency.format(amount);
    }

    function renderProducts() {
        byId('result-count').textContent = `${totalProducts.toLocaleString(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO')} ${totalProducts === 1 ? t('product') : t('products')}`;
        grid.setAttribute('aria-busy', 'false');
        byId('empty-state').hidden = products.length !== 0;
        byId('catalog-pagination').hidden = totalPages <= 1;
        byId('previous-page').disabled = currentPage <= 1;
        byId('next-page').disabled = currentPage >= totalPages;
        byId('pagination-summary').textContent = totalPages
            ? `${t('page')} ${currentPage} ${t('of')} ${totalPages}`
            : t('noResults');
        grid.hidden = products.length === 0;
        grid.innerHTML = products.map((product, index) => `
            <article class="product-card" style="animation-delay:${Math.min(index * 45, 270)}ms">
                ${product.purchase_available === false ? '' : `<a class="product-card-link" data-product-view="${escapeHtml(product.id)}" href="/producto/${encodeURIComponent(product.id)}" aria-label="${escapeHtml(product.name)}"></a>`}
                <div class="product-image-wrap">
                    <img class="product-image" src="${imagePath(product.image)}" alt="${escapeHtml(product.name)}" loading="lazy">
                    <span class="product-badge">${product.purchase_available === false ? t('externalCatalog') : product.flash_sale_percent ? `${t('offers')} ${product.flash_sale_percent}%` : escapeHtml(product.badge)}</span>
                    ${product.purchase_available === false
                        ? `<span class="add-button reference-badge" title="${t('referenceOnly')}"><i class="fa-solid fa-circle-info" aria-hidden="true"></i></span>`
                        : `<button class="add-button" type="button" data-add="${escapeHtml(product.id)}" aria-label="${t('addToBag')} ${escapeHtml(product.name)}"><i class="fa-solid fa-cart-shopping"></i></button>`}
                </div>
                <div class="product-info">
                    <p class="product-category">${escapeHtml(product.category_name || window.PemuI18n.category(product.category))}${product.seller_name ? ` · ${escapeHtml(product.external_catalog ? t('referenceSeller') : product.seller_name)}` : ''}</p>
                    <h3 title="${escapeHtml(product.name)}">${escapeHtml(product.name)}</h3>
                    <p class="product-description">${escapeHtml(product.description)}</p>
                    ${product.flash_sale_percent ? `<p class="sale-countdown" data-countdown="${escapeHtml(product.flash_sale_ends_at)}"><i class="fa-regular fa-clock"></i> ${t('saleLimited')}</p>` : ''}
                    ${product.low_stock ? `<p class="stock-note">${window.PemuI18n.language === 'en' ? 'Only' : 'Quedan'} ${product.stock} ${product.stock === 1 ? t('unit') : t('units')}</p>` : ''}
                    ${product.source_url ? `<a class="product-source" href="${escapeHtml(product.source_url)}" target="_blank" rel="noopener noreferrer">${product.purchase_available === false ? t('externalReference') : t('originCatalog')} <i class="fa-solid fa-arrow-up-right-from-square"></i></a>` : ''}
                    ${product.purchase_available === false ? `<p class="reference-note">${t('sampleItem')}</p>` : ''}
                    <div class="product-prices"><strong class="current-price">${productPrice(product, product.price)}</strong>${product.original_price > product.price ? `<span class="old-price">${productPrice(product, product.original_price)}</span>` : ''}</div>
                </div>
            </article>`).join('');
        clearInterval(countdownTimer);
        countdownTimer = setInterval(() => {
            let expired = false;
            grid.querySelectorAll('[data-countdown]').forEach((element) => {
                const remaining = new Date(element.dataset.countdown).getTime() - Date.now();
                if (!Number.isFinite(remaining) || remaining <= 0) {
                    expired = true;
                    return;
                }
                const hours = Math.floor(remaining / 3600000);
                const minutes = Math.floor((remaining % 3600000) / 60000);
                const seconds = Math.floor((remaining % 60000) / 1000);
                element.textContent = `${t('saleEnds')} ${hours}h ${String(minutes).padStart(2, '0')}m ${String(seconds).padStart(2, '0')}s`;
            });
            if (expired) {
                clearInterval(countdownTimer);
                loadProducts(currentPage).catch((error) => showToast(error.message));
            }
        }, 1000);
    }

    async function loadProducts(page = 1) {
        const requestId = ++catalogRequestId;
        currentPage = page;
        renderSkeletons();
        const params = new URLSearchParams(window.location.search);
        const requestParams = new URLSearchParams({
            page: String(page),
            limit: String(pageSize),
            sort: byId('sort-products').value,
            lang: window.PemuI18n.language,
        });
        const category = byId('category-filter').value || (requestId === 1 ? params.get('categoria') || '' : '');
        const search = byId('product-search').value.trim();
        if (category) requestParams.set('category', category);
        if (search) requestParams.set('q', search);
        if (params.get('ofertas') === '1') requestParams.set('offers', '1');
        const data = await api(`/api/productos?${requestParams.toString()}`);
        if (requestId !== catalogRequestId) return;
        products = data.products;
        totalProducts = data.total;
        totalPages = data.total_pages;
        currentPage = data.page;
        byId('catalog-warning').hidden = !data.warning;
        byId('catalog-warning').textContent = data.warning || '';
        const exchangeRateNote = byId('exchange-rate-note');
        exchangeRateNote.hidden = !data.exchange_rate;
        exchangeRateNote.textContent = data.exchange_rate
            ? `${t('pricesConverted')} · 1 USD = ${Number(data.exchange_rate).toLocaleString('en-US', { maximumFractionDigits: 2 })} COP.`
            : '';
        if (currentUser?.personalization_enabled) {
            try {
                const recommendations = await api('/api/products/recommendations');
                const order = new Map(recommendations.products.map((product, index) => [product.id, index]));
                products.sort((first, second) => (order.get(first.id) ?? Number.MAX_SAFE_INTEGER)
                    - (order.get(second.id) ?? Number.MAX_SAFE_INTEGER));
            } catch (error) {
                console.warn('No se pudieron cargar las recomendaciones:', error);
                byId('catalog-warning').hidden = false;
                byId('catalog-warning').textContent = [
                    data.warning,
                    t('recommendationFailure'),
                ].filter(Boolean).join(' ');
            }
            if (requestId !== catalogRequestId) return;
            if (params.get('ofertas') !== '1') {
                byId('catalog-eyebrow').textContent = t('personalized');
                byId('catalog-title').textContent = t('pickedForYou');
            }
        } else {
            byId('catalog-eyebrow').textContent = t('todaysSelection');
            byId('catalog-title').textContent = t('worthwhile');
        }
        const categoryFilter = byId('category-filter');
        const selectedCategory = categoryFilter.value || (requestId === 1 ? params.get('categoria') || '' : '');
        categoryFilter.innerHTML = `<option value="">${t('allCategories')}</option>`
            + data.categories.map((category) => `<option value="${escapeHtml(category)}">${escapeHtml(window.PemuI18n.category(category))}</option>`).join('');
        if (data.categories.includes(selectedCategory)) categoryFilter.value = selectedCategory;
        byId('categories-dropdown').innerHTML = `<a href="/#catalogo" role="menuitem">${t('allCategories')}</a>`
            + data.categories.map((category) => `<a href="/?categoria=${encodeURIComponent(category)}#catalogo" role="menuitem">${escapeHtml(window.PemuI18n.category(category))}</a>`).join('');
        if (params.get('ofertas') === '1') byId('catalog-title').textContent = t('offersForYou');
        byId('clear-search').hidden = !search;
        renderProducts();
        if (currentUser?.personalization_enabled && search) {
            const productIds = products.filter((product) => product.purchase_available !== false).slice(0, 5).map((product) => product.id);
            if (productIds.length) {
                api('/api/product-interactions', {
                    method: 'POST',
                    body: JSON.stringify({ action: 'search_result', product_ids: productIds }),
                }).catch((error) => console.warn('No se pudieron guardar los resultados de búsqueda:', error));
            }
        }
    }

    function setCart(data) {
        if (appliedCoupon && appliedCoupon.subtotal !== data.total) {
            appliedCoupon = null;
            byId('coupon-summary').hidden = true;
            byId('coupon-message').textContent = 'La bolsa cambió. Vuelve a aplicar el cupón para actualizar el descuento.';
            byId('coupon-message').className = 'coupon-message';
        }
        cart = data;
        byId('cart-count').textContent = String(cart.count);
        byId('drawer-count').textContent = `(${cart.count})`;
        byId('cart-empty').hidden = cart.items.length > 0;
        byId('drawer-bottom').hidden = cart.items.length === 0;
        byId('cart-total').textContent = currency.format(cart.total);
        byId('cart-items').innerHTML = cart.items.map((item) => `
            <article class="cart-item">
                <img src="${imagePath(item.image)}" alt="">
                <div><h3>${escapeHtml(item.name)}</h3><p>${currency.format(item.price)}</p>
                    <div class="quantity-control">
                        <button type="button" data-quantity="${escapeHtml(item.id)}" data-value="${item.quantity - 1}" aria-label="${window.PemuI18n.language === 'en' ? 'Remove one item' : 'Quitar una unidad'}">−</button>
                        <span>${item.quantity}</span>
                        <button type="button" data-quantity="${escapeHtml(item.id)}" data-value="${item.quantity + 1}" aria-label="${window.PemuI18n.language === 'en' ? 'Add one item' : 'Añadir una unidad'}" ${item.quantity >= item.stock ? 'disabled' : ''}>+</button>
                    </div>
                </div>
                <button class="remove-item" type="button" data-quantity="${escapeHtml(item.id)}" data-value="0" aria-label="${window.PemuI18n.language === 'en' ? 'Remove' : 'Quitar'} ${escapeHtml(item.name)}"><i class="fa-regular fa-trash-can"></i></button>
            </article>`).join('');
    }

    function openDrawer() {
        drawer.classList.add('open');
        drawer.setAttribute('aria-hidden', 'false');
        scrim.hidden = false;
        requestAnimationFrame(() => scrim.classList.add('open'));
        document.body.classList.add('drawer-open');
        byId('close-cart').focus();
    }

    function closeDrawer() {
        drawer.classList.remove('open');
        drawer.setAttribute('aria-hidden', 'true');
        scrim.classList.remove('open');
        document.body.classList.remove('drawer-open');
        setTimeout(() => { if (!drawer.classList.contains('open')) scrim.hidden = true; }, 240);
    }

    function setUser(user) {
        currentUser = user;
        byId('nav-account-label').textContent = user ? user.name.split(' ')[0] : t('account');
        byId('delivery-name').value = user?.name || '';
        byId('delivery-phone').value = user?.phone || '';
        byId('delivery-address').value = user?.address || '';
        byId('delivery-department').value = user?.department || '';
        byId('delivery-city').value = user?.city || '';
        byId('delivery-postal-code').value = user?.postal_code || '';
        byId('delivery-notes').value = user?.delivery_notes || '';
        if (user) {
            byId('checkout-form').querySelectorAll('input, button').forEach((control) => { control.disabled = false; });
        }
        byId('sign-in-hint').hidden = Boolean(user);
        byId('checkout-form').querySelectorAll('input, button').forEach((control) => { control.disabled = !user; });
    }

    function redirectToProductSignIn(productId, quantity) {
        const returnPath = `/producto/${encodeURIComponent(productId)}?add=${encodeURIComponent(quantity)}`;
        window.location.assign(`/cuenta?next=${encodeURIComponent(returnPath)}`);
    }

    grid.addEventListener('click', async (event) => {
        const productLink = event.target.closest('[data-product-view]');
        if (productLink && currentUser?.personalization_enabled) {
            api('/api/product-interactions', {
                method: 'POST',
                body: JSON.stringify({ action: 'view', product_ids: [productLink.dataset.productView] }),
                keepalive: true,
            }).catch((error) => console.warn('No se pudo actualizar la recomendación:', error));
        }
        const button = event.target.closest('[data-add]');
        if (!button) return;
        const item = cart.items.find((entry) => entry.id === button.dataset.add);
        if (!currentUser) {
            redirectToProductSignIn(button.dataset.add, 1);
            return;
        }
        const quantity = (item?.quantity || 0) + 1;
        button.disabled = true;
        try {
            setCart(await api('/api/cart', { method: 'POST', body: JSON.stringify({ product_id: button.dataset.add, quantity }) }));
            showToast('Añadido a tu bolsa');
        } catch (error) {
            if (error.status === 401) redirectToProductSignIn(button.dataset.add, 1);
            else showToast(error.message);
        } finally {
            button.disabled = false;
        }
    });

    byId('cart-items').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-quantity]');
        if (!button) return;
        try {
            setCart(await api('/api/cart', { method: 'POST', body: JSON.stringify({ product_id: button.dataset.quantity, quantity: Number(button.dataset.value) }) }));
        } catch (error) {
            showToast(error.message);
        }
    });

    function requestCatalog(page = 1) {
        const requestId = catalogRequestId + 1;
        loadProducts(page).catch((error) => {
            if (requestId !== catalogRequestId) return;
            grid.setAttribute('aria-busy', 'false');
            grid.hidden = false;
            grid.innerHTML = `<p class="loading-state">${t('catalogFailure')} ${escapeHtml(error.message)}</p>`;
            byId('catalog-pagination').hidden = true;
        });
    }

    byId('category-filter').addEventListener('change', () => requestCatalog(1));
    document.addEventListener('pemu-language-change', () => {
        currency = new Intl.NumberFormat(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO', {
            style: 'currency',
            currency: 'COP',
            currencyDisplay: 'code',
            maximumFractionDigits: 0,
        });
        setCart(cart);
        if (!currentUser) byId('nav-account-label').textContent = t('account');
        requestCatalog(1);
    });
    byId('product-search').addEventListener('input', () => {
        byId('clear-search').hidden = !byId('product-search').value;
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => requestCatalog(1), 300);
    });
    byId('clear-search').addEventListener('click', () => {
        byId('product-search').value = '';
        byId('product-search').dispatchEvent(new Event('input'));
        byId('product-search').focus();
    });
    byId('sort-products').addEventListener('change', () => requestCatalog(1));
    byId('previous-page').addEventListener('click', () => {
        if (currentPage > 1) requestCatalog(currentPage - 1);
    });
    byId('next-page').addEventListener('click', () => {
        if (currentPage < totalPages) requestCatalog(currentPage + 1);
    });
    byId('reset-filters').addEventListener('click', () => {
        byId('category-filter').value = '';
        byId('product-search').value = '';
        byId('nav-search').value = '';
        byId('clear-search').hidden = true;
        requestCatalog(1);
    });
    byId('open-cart').addEventListener('click', openDrawer);
    function toggleMenu(menuId, triggerId, open) {
        const menu = byId(menuId);
        const trigger = byId(triggerId);
        const shouldOpen = open ?? menu.hidden;
        menu.hidden = !shouldOpen;
        trigger.setAttribute('aria-expanded', String(shouldOpen));
    }
    byId('categories-toggle').addEventListener('click', () => toggleMenu('categories-dropdown', 'categories-toggle'));
    byId('help-toggle').addEventListener('click', () => toggleMenu('help-dropdown', 'help-toggle'));
    document.addEventListener('click', (event) => {
        if (!event.target.closest('#categories-menu')) toggleMenu('categories-dropdown', 'categories-toggle', false);
        if (!event.target.closest('#help-menu')) toggleMenu('help-dropdown', 'help-toggle', false);
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            toggleMenu('categories-dropdown', 'categories-toggle', false);
            toggleMenu('help-dropdown', 'help-toggle', false);
        }
    });
    byId('nav-search-form').addEventListener('submit', (event) => {
        event.preventDefault();
        byId('product-search').value = byId('nav-search').value;
        byId('clear-search').hidden = !byId('product-search').value;
        requestCatalog(1);
        byId('catalogo').scrollIntoView({ behavior: 'smooth' });
        byId('product-search').focus({ preventScroll: true });
    });
    byId('close-cart').addEventListener('click', closeDrawer);
    byId('continue-shopping').addEventListener('click', closeDrawer);
    scrim.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDrawer(); });

    byId('checkout-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = event.currentTarget.querySelector('button[type="submit"]');
        const message = byId('checkout-message');
        message.textContent = '';
        button.disabled = true;
        try {
            const formData = new FormData(event.currentTarget);
            const checkoutData = Object.fromEntries(formData);
            if (checkoutData.coupon_code && appliedCoupon?.code !== checkoutData.coupon_code.trim().toUpperCase()) {
                throw new Error('Aplica y revisa el cupón antes de continuar.');
            }
            checkoutData.coupon_code = checkoutData.coupon_code.trim().toUpperCase();
            const result = await api('/api/checkout', { method: 'POST', body: JSON.stringify(checkoutData) });
            window.location.assign(result.payment_url);
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = !currentUser;
        }
    });

    byId('apply-coupon').addEventListener('click', async () => {
        const input = byId('discount-coupon-code');
        const message = byId('coupon-message');
        const summary = byId('coupon-summary');
        const code = input.value.trim().toUpperCase();
        appliedCoupon = null;
        summary.hidden = true;
        message.className = 'coupon-message';
        if (!code) {
            message.textContent = 'Escribe el código de tu cupón.';
            return;
        }
        try {
            const quote = await api(`/api/coupons/quote?code=${encodeURIComponent(code)}`);
            appliedCoupon = quote;
            input.value = quote.code;
            message.textContent = 'Cupón aplicado. El descuento se repartirá entre los vendedores.';
            message.classList.add('success');
            summary.textContent = `Subtotal: ${currency.format(quote.subtotal)} · Descuento: −${currency.format(quote.discount)} · Total estimado: ${currency.format(quote.total)}`;
            summary.hidden = false;
        } catch (error) {
            message.textContent = error.message;
            message.classList.add('error');
        }
    });

    (async () => {
        try {
            const params = new URLSearchParams(window.location.search);
            byId('product-search').value = params.get('q') || params.get('buscar') || '';
            byId('nav-search').value = byId('product-search').value;
            byId('clear-search').hidden = !byId('product-search').value;
            const [sessionData, cartData] = await Promise.all([api('/api/session'), api('/api/cart')]);
            csrfToken = sessionData.csrf_token;
            setUser(sessionData.user);
            setCart(cartData);
            await loadProducts(1);
        } catch (error) {
            grid.setAttribute('aria-busy', 'false');
            grid.innerHTML = `<p class="loading-state">${t('storeFailure')} ${escapeHtml(error.message)}<br>${window.PemuI18n.language === 'en' ? 'Check that the Python application is running.' : 'Comprueba que la aplicación Python esté en marcha.'}</p>`;
        }
    })();
});