document.addEventListener('DOMContentLoaded', () => {
    let csrfToken = '';
    let previewProducts = [];
    let toastTimer;
    const byId = (id) => document.getElementById(id);
    const t = (key) => window.PemuI18n.t(key);
    const currency = new Intl.NumberFormat(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO', {
        style: 'currency', currency: 'COP', currencyDisplay: 'code', maximumFractionDigits: 0,
    });
    const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);

    document.addEventListener('error', (event) => {
        const image = event.target;
        if (image instanceof HTMLImageElement && /^https:\/\//i.test(image.src) && !image.dataset.fallback) {
            image.dataset.fallback = 'true';
            image.src = '/imagenes/img1.jpg';
        }
    }, true);

    async function api(path, options = {}) {
        const headers = { ...(options.headers || {}) };
        if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
        if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
        const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo completar la solicitud.');
        return data;
    }

    function imagePath(path) {
        return /^https:\/\//i.test(path) ? path : `/${path.split('/').map(encodeURIComponent).join('/')}`;
    }

    function showToast(message) {
        const toast = byId('seller-toast');
        toast.textContent = message;
        toast.classList.add('show');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => toast.classList.remove('show'), 2600);
    }

    function showFormMessage(id, message, success = false) {
        const element = byId(id);
        element.textContent = message;
        element.classList.toggle('success', success);
    }

    document.addEventListener('pemu-language-change', () => window.location.reload());

    async function loadListings() {
        const container = byId('seller-products');
        const data = await api('/api/seller/products');
        byId('listing-count').textContent = `${data.products.length} ${data.products.length === 1 ? t('productCount') : t('productsCount')}`;
        if (!data.products.length) {
            container.innerHTML = `<p class="empty-listings">${t('noProductsPublished')}</p>`;
            return;
        }
        container.innerHTML = data.products.map((product) => `
            <article class="listing-item" data-product="${escapeHtml(product.id)}">
                <img class="listing-image" src="${imagePath(product.image)}" alt="">
                <div class="listing-summary">
                    <h4 class="listing-name">${escapeHtml(product.name)}</h4>
                    <p>${escapeHtml(window.PemuI18n.category(product.category))} · ${product.active ? (product.stock ? `${product.stock} ${t('available')}` : t('noStock')) : t('removed')}</p>
                    ${product.active && product.stock ? `<a href="/producto/${encodeURIComponent(product.id)}" target="_blank" rel="noopener noreferrer">${t('showListing')}</a>` : ''}
                    ${product.source_url ? `<a href="${escapeHtml(product.source_url)}" target="_blank" rel="noopener noreferrer">${t('sourceLabel')} ${escapeHtml(product.external_catalog || t('externalCatalog'))}</a>` : ''}
                </div>
                <form class="listing-editor" data-save="${escapeHtml(product.id)}">
                    <label>${t('basePrice')}<input name="price" type="number" min="1" max="10000000000" step="1" value="${product.base_price}" required></label>
                    <label>${t('inventory')}<input name="stock" type="number" min="0" max="1000" step="1" value="${product.stock}" required></label>
                    <label>${t('flashDiscount')}<select name="sale_percent">
                        ${[0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70].map((percent) => `<option value="${percent}" ${product.flash_sale_percent === percent ? 'selected' : ''}>${percent ? `${percent}%` : t('noOffer')}</option>`).join('')}
                    </select></label>
                    <label>${t('duration')}<select name="sale_hours">
                        ${[1, 6, 12, 24, 48, 72].map((hours) => `<option value="${hours}" ${hours === 24 ? 'selected' : ''}>${hours} h</option>`).join('')}
                    </select></label>
                    <div class="listing-actions">
                        <button type="submit"><i class="fa-regular fa-floppy-disk"></i> ${t('save')}</button>
                        <button class="unlist-button" type="button" data-unlist="${escapeHtml(product.id)}"><i class="fa-regular fa-eye-slash"></i> ${t('withdraw')}</button>
                    </div>
                </form>
            </article>`).join('');
    }

    async function loadNequiOrders() {
        const data = await api('/api/seller/nequi-orders');
        byId('commission-due').textContent = `${t('pendingCommission')}: ${currency.format(data.commission_due)}`;
        byId('nequi-orders-list').innerHTML = data.orders.length
            ? data.orders.map((order) => `
                <article class="nequi-order-card">
                    <div>
                        <h4>${t('order')} #${order.order_id} · ${escapeHtml(order.customer_name)}</h4>
                        <p>${escapeHtml(order.customer_phone)} · ${escapeHtml(order.address)}, ${escapeHtml(order.city)}</p>
                        <p>${t('transferReceived')}: ${currency.format(order.amount)} · ${t('pemuCommissionPending')}: ${currency.format(order.commission_due)}</p>
                    </div>
                    <strong>${currency.format(order.amount)}</strong>
                    <ul>${order.items.map((item) => `<li>${item.quantity} × ${escapeHtml(item.product_name)}</li>`).join('')}</ul>
                    <button class="secondary-button" type="button" data-confirm-nequi="${escapeHtml(order.split_id)}">${t('confirmPaymentReceived')}</button>
                </article>`).join('')
            : `<p class="empty-listings">${t('noPendingNequi')}</p>`;
        byId('nequi-orders-panel').hidden = !data.orders.length && !data.commission_due;
    }

    async function loadSellerOrders() {
        const data = await api('/api/seller/orders');
        const container = byId('seller-shipping-list');
        container.innerHTML = data.orders.length
            ? data.orders.map((order) => `
                <article class="seller-shipping-card">
                    <div class="seller-shipping-heading">
                        <strong>${t('order')} #${order.order_id} · ${escapeHtml(order.customer_name)}</strong>
                        <span>${t(order.shipping_status === 'delivered' ? 'delivered' : order.shipping_status === 'in_transit' ? 'inTransit' : order.shipping_status === 'shipped' ? 'shipped' : 'preparing')}</span>
                    </div>
                    <p>${escapeHtml(order.phone)} · ${escapeHtml(order.address)}, ${escapeHtml(order.city)}, ${escapeHtml(order.department)}</p>
                    <ul>${order.items.map((item) => `<li>${item.quantity} × ${escapeHtml(item.product_name)}</li>`).join('')}</ul>
                    ${order.delivered_at ? `<p>${t('delivered')}: ${new Date(order.delivered_at).toLocaleString(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO')}</p>` : ''}
                    <form class="tracking-form" data-tracking="${escapeHtml(order.split_id)}">
                        <label>${t('status')}<select name="status" ${order.delivered_at ? 'disabled' : ''}>
                            ${[['preparing', 'preparing'], ['shipped', 'shipped'], ['in_transit', 'inTransit'], ['delivered', 'delivered']].map(([value, key]) => `<option value="${value}" ${order.shipping_status === value ? 'selected' : ''}>${t(key)}</option>`).join('')}
                        </select></label>
                        <label>${t('carrier')}<input name="carrier" maxlength="80" value="${escapeHtml(order.shipping_carrier || '')}" placeholder="${t('carrierName')}"></label>
                        <label>${t('trackingNumber')}<input name="tracking_number" maxlength="120" value="${escapeHtml(order.tracking_number || '')}" placeholder="${t('trackingPlaceholder')}"></label>
                        <label>${t('estimatedDelivery')}<input name="estimated_delivery_at" type="date" value="${escapeHtml(order.estimated_delivery_at || '')}"></label>
                        <button type="submit" ${order.delivered_at ? 'disabled' : ''}>${t('saveTracking')}</button>
                    </form>
                </article>`).join('')
            : `<p class="empty-listings">${t('noOrdersToPrepare')}</p>`;
    }

    async function loadSellerReturns() {
        const data = await api('/api/seller/returns');
        const container = byId('seller-returns-list');
        const reasons = {
            damaged: t('returnReasonDamaged'),
            incomplete: t('returnReasonIncomplete'),
            not_as_described: t('returnReasonMismatch'),
            other: t('otherReason'),
        };
        container.innerHTML = data.requests.length
            ? data.requests.map((item) => `
                <article class="seller-return-card">
                    <div><strong>${t('order')} #${item.order_id} · ${escapeHtml(item.product_name)}</strong><p>${escapeHtml(item.customer_name)} · ${escapeHtml(item.phone)}</p>
                        <p>${reasons[item.reason] || t('otherReason')}${item.details ? ` · ${escapeHtml(item.details)}` : ''}</p>
                        <small>${item.free_return ? (window.PemuI18n.language === 'en' ? 'First return for this order: free; coordinate the return.' : 'Primera devolución del pedido: sin costo; coordina el retorno.') : (window.PemuI18n.language === 'en' ? 'Return shipping cost is reviewed with the customer.' : 'El costo del retorno se revisa con el cliente.')}</small>
                    </div>
                    <label>${t('status')}<select data-return-status="${item.id}" ${['accepted', 'rejected'].includes(item.status) ? 'disabled' : ''}>
                        ${[['received', 'returnReceived'], ['reviewing', 'reviewing'], ['accepted', 'accepted'], ['rejected', 'rejected']].map(([value, key]) => `<option value="${value}" ${item.status === value ? 'selected' : ''}>${t(key)}</option>`).join('')}
                    </select></label>
                    ${['accepted', 'rejected'].includes(item.status) ? '' : `<button type="button" data-save-return="${item.id}">${t('updateStatus')}</button>`}
                </article>`).join('')
            : `<p class="empty-listings">${t('noReturns')}</p>`;
    }

    function renderPreview(products) {
        previewProducts = products;
        byId('preview-count').textContent = `${products.length} ${products.length === 1 ? t('productCount') : t('productsCount')}`;
        byId('preview-list').innerHTML = products.map((product, index) => `
            <article class="preview-item" data-preview="${index}">
                <img class="preview-image" src="${product.image_url ? imagePath(product.image_url) : '/imagenes/black-wireless-headphones-isolated-on-transparent-background-ai-generated-png.webp'}" alt="">
                <div>
                    <h4>${escapeHtml(product.name)}</h4>
                    <p>${escapeHtml(product.description)} · ${t('reference')} ${currency.format(product.reference_price)}</p>
                    <a class="preview-source" href="${escapeHtml(product.source_url)}" target="_blank" rel="noopener noreferrer">${t('sourceProduct')}</a>
                </div>
                <div class="preview-price">
                    <label for="sell-price-${index}">${t('salePrice')}</label>
                    <input id="sell-price-${index}" class="preview-seller-price" type="number" min="1" max="10000000000" step="1" value="${product.reference_price}">
                    <label for="sell-stock-${index}">${t('availableUnitsLabel')}</label>
                    <input id="sell-stock-${index}" class="preview-stock" type="number" min="1" max="1000" step="1" value="1">
                    <button class="preview-publish" type="button" data-publish="${index}"><i class="fa-solid fa-plus"></i> ${t('publish')}</button>
                </div>
            </article>`).join('');
        byId('external-preview').hidden = false;
    }

    document.querySelectorAll('.seller-tab').forEach((tab) => tab.addEventListener('click', () => {
        const showCatalog = tab.id === 'catalog-tab';
        byId('manual-panel').hidden = showCatalog;
        byId('catalog-panel').hidden = !showCatalog;
        byId('manual-tab').classList.toggle('active', !showCatalog);
        byId('manual-tab').setAttribute('aria-selected', String(!showCatalog));
        byId('catalog-tab').classList.toggle('active', showCatalog);
        byId('catalog-tab').setAttribute('aria-selected', String(showCatalog));
    }));

    byId('product-image').addEventListener('change', (event) => {
        const file = event.target.files[0];
        byId('product-image-name').textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB` : t('noFileSelected');
    });

    byId('nequi-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = event.currentTarget.querySelector('button[type="submit"]');
        button.disabled = true;
        showFormMessage('nequi-message', '');
        try {
            const result = await api('/api/seller/nequi', {
                method: 'PUT',
                body: JSON.stringify({ phone: byId('seller-nequi-phone').value }),
            });
            byId('seller-nequi-phone').value = result.nequi_phone || '';
            showFormMessage('nequi-message', result.nequi_phone ? 'Número Nequi guardado. Ya puedes publicar y recibir pagos directos.' : 'Número Nequi eliminado.', true);
            await loadListings();
        } catch (error) {
            showFormMessage('nequi-message', error.message);
        } finally {
            button.disabled = false;
        }
    });

    byId('nequi-orders-list').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-confirm-nequi]');
        if (!button) return;
        button.disabled = true;
        try {
            await api(`/api/seller/nequi-orders/${encodeURIComponent(button.dataset.confirmNequi)}/confirm`, { method: 'POST', body: '{}' });
            showToast('Pago Nequi confirmado; la comisión quedó pendiente.');
            await Promise.all([loadNequiOrders(), loadListings(), loadSellerOrders()]);
        } catch (error) {
            showToast(error.message);
            button.disabled = false;
        }
    });

    byId('seller-shipping-list').addEventListener('submit', async (event) => {
        const form = event.target.closest('[data-tracking]');
        if (!form) return;
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]');
        button.disabled = true;
        try {
            const values = Object.fromEntries(new FormData(form));
            await api(`/api/seller/orders/${encodeURIComponent(form.dataset.tracking)}/tracking`, {
                method: 'PATCH',
                body: JSON.stringify(values),
            });
            showToast('Seguimiento actualizado.');
            await loadSellerOrders();
        } catch (error) {
            showToast(error.message);
            button.disabled = false;
        }
    });

    byId('seller-returns-list').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-save-return]');
        if (!button) return;
        button.disabled = true;
        try {
            const status = byId('seller-returns-list').querySelector(`[data-return-status="${CSS.escape(button.dataset.saveReturn)}"]`).value;
            await api(`/api/seller/returns/${encodeURIComponent(button.dataset.saveReturn)}`, {
                method: 'PATCH',
                body: JSON.stringify({ status }),
            });
            showToast('Estado de la solicitud actualizado.');
            await loadSellerReturns();
        } catch (error) {
            showToast(error.message);
            button.disabled = false;
        }
    });

    byId('product-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const button = form.querySelector('button[type="submit"]');
        showFormMessage('product-message', '');
        button.disabled = true;
        try {
            const result = await api('/api/seller/products', { method: 'POST', body: new FormData(form) });
            form.reset();
            byId('product-image-name').textContent = 'Ningún archivo seleccionado';
            showFormMessage('product-message', 'Tu producto ya está publicado.', true);
            await loadListings();
            showToast(`${result.product.name} ya está en la tienda`);
        } catch (error) {
            showFormMessage('product-message', error.message);
        } finally {
            button.disabled = false;
        }
    });

    byId('catalog-file').addEventListener('change', (event) => {
        const file = event.target.files[0];
        byId('catalog-file-name').textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB` : t('noFileCatalog');
        byId('external-preview').hidden = true;
        previewProducts = [];
    });

    byId('catalog-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const button = event.currentTarget.querySelector('button[type="submit"]');
        showFormMessage('catalog-message', '');
        button.disabled = true;
        try {
            const data = await api('/api/seller/catalog/preview', { method: 'POST', body: new FormData(event.currentTarget) });
            renderPreview(data.products);
        } catch (error) {
            showFormMessage('catalog-message', error.message);
        } finally {
            button.disabled = false;
        }
    });

    byId('preview-list').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-publish]');
        if (!button) return;
        const index = Number(button.dataset.publish);
        const product = previewProducts[index];
        const card = button.closest('.preview-item');
        const price = Number(card.querySelector('.preview-seller-price').value);
        const stock = Number(card.querySelector('.preview-stock').value);
        button.disabled = true;
        try {
            const result = await api('/api/seller/products', {
                method: 'POST',
                body: JSON.stringify({ ...product, price, stock }),
            });
            card.remove();
            previewProducts[index] = null;
            byId('preview-count').textContent = `${byId('preview-list').children.length} ${t('countPending')}`;
            await loadListings();
            showToast(`${result.product.name} ya está en la tienda`);
        } catch (error) {
            showFormMessage('catalog-message', error.message);
            button.disabled = false;
        }
    });

    byId('seller-products').addEventListener('submit', async (event) => {
        const form = event.target.closest('[data-save]');
        if (!form) return;
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]');
        button.disabled = true;
        try {
            const formData = new FormData(form);
            const result = await api(`/api/seller/products/${encodeURIComponent(form.dataset.save)}`, {
                method: 'PATCH',
                body: JSON.stringify({
                    price: Number(formData.get('price')),
                    stock: Number(formData.get('stock')),
                    sale_percent: Number(formData.get('sale_percent')),
                    sale_hours: Number(formData.get('sale_hours')),
                }),
            });
            showToast('Precio y existencias actualizados');
            await loadListings();
        } catch (error) {
            showToast(error.message);
        } finally {
            button.disabled = false;
        }
    });

    byId('seller-products').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-unlist]');
        if (!button) return;
        button.disabled = true;
        try {
            await api(`/api/seller/products/${encodeURIComponent(button.dataset.unlist)}`, { method: 'DELETE' });
            await loadListings();
            showToast('Producto retirado de la tienda');
        } catch (error) {
            showToast(error.message);
            button.disabled = false;
        }
    });

    api('/api/session').then(async (data) => {
        csrfToken = data.csrf_token;
        if (!data.user) {
            byId('seller-gate').hidden = false;
            return;
        }
        byId('seller-workspace').hidden = false;
        byId('seller-greeting').textContent = `${window.PemuI18n.language === 'en' ? 'Hello' : 'Hola'}, ${data.user.name.split(' ')[0]}`;
        byId('seller-email').textContent = data.user.email;
        const payoutStatus = await api('/api/seller/payout-account');
        byId('seller-nequi-phone').value = payoutStatus.nequi_phone || '';
        if (payoutStatus.nequi_phone) {
            byId('payout-status').textContent = window.PemuI18n.language === 'en'
                ? `Customers transfer directly to Nequi ${payoutStatus.nequi_phone}. Confirm each payment after it appears in your account; XrteShop's 10% commission remains due.`
                : `Los clientes transfieren directamente al Nequi ${payoutStatus.nequi_phone}. Confirma cada pago cuando lo veas reflejado; la comisión de XrteShop del 10% queda pendiente por pagar.`;
        }
        if (payoutStatus.connected) {
            if (!payoutStatus.nequi_phone) {
                byId('payout-status').textContent = window.PemuI18n.language === 'en'
                    ? 'Mercado Pago is also connected; customers can pay at checkout and the payment provider will process the transfer.'
                    : 'También tienes Mercado Pago conectado; los clientes podrán pagar desde su checkout y el procesador gestionará el abono.';
            }
            byId('payout-connect').hidden = true;
        }
        const paymentState = new URLSearchParams(window.location.search).get('mp');
        const paymentMessages = {
            connected: 'Tu cuenta de Mercado Pago quedó conectada.',
            'configuration-error': 'Falta configurar Mercado Pago en XrteShop con las credenciales de la aplicación y un dominio HTTPS público.',
            'authorization-cancelled': 'Se canceló la conexión con Mercado Pago.',
            'authorization-error': 'No se pudo validar la conexión con Mercado Pago. Inténtalo de nuevo.',
        };
        if (paymentMessages[paymentState]) showToast(paymentMessages[paymentState]);
        await Promise.all([loadListings(), loadNequiOrders(), loadSellerOrders(), loadSellerReturns()]);
    }).catch((error) => {
        byId('seller-gate').hidden = false;
        byId('seller-gate').querySelector('p').textContent = `${error.message} Comprueba que la aplicación Python esté en marcha.`;
    });
});