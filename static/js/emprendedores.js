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

    function variantText(variants) {
        return (variants || []).map((variant) => {
            const options = Object.entries(variant.options)
                .map(([name, value]) => `${name}=${value}`)
                .join('; ');
            return `${options} | ${variant.stock}${variant.price ? ` | ${variant.price}` : ''}`;
        }).join('\n');
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
                <div class="listing-overview">
                    <div class="listing-image-wrap"><img class="listing-image" src="${imagePath(product.image)}" alt="${escapeHtml(product.name)}"></div>
                    <div class="listing-summary">
                        <span class="listing-category">${escapeHtml(window.PemuI18n.category(product.category))}</span>
                        <h4 class="listing-name">${escapeHtml(product.name)}</h4>
                        <p class="listing-stock ${product.stock ? '' : 'is-empty'}"><span></span>${product.stock ? `${product.stock} ${t('available')}` : t('noStock')}</p>
                        ${product.active && product.stock ? `<a href="/producto/${encodeURIComponent(product.id)}" target="_blank" rel="noopener noreferrer"><i class="fa-regular fa-eye"></i> ${t('showListing')}</a>` : ''}
                        ${product.source_url ? `<a href="${escapeHtml(product.source_url)}" target="_blank" rel="noopener noreferrer">${t('sourceLabel')} ${escapeHtml(product.external_catalog || t('externalCatalog'))}</a>` : ''}
                    </div>
                </div>
                <form class="listing-editor" data-save="${escapeHtml(product.id)}">
                    <div class="listing-editor-heading"><div><span>${t('listingEditorEyebrow')}</span><h4>${t('listingEditorTitle')}</h4></div><i class="fa-regular fa-pen-to-square" aria-hidden="true"></i></div>
                    <label class="listing-field-wide">${t('productName')}<input name="name" type="text" minlength="3" maxlength="100" value="${escapeHtml(product.name)}" required></label>
                    <label class="listing-field-wide">${t('optionalDescription')}<textarea name="description" maxlength="500" rows="3">${escapeHtml(product.description)}</textarea></label>
                    <label>${t('basePrice')}<input name="price" type="number" min="1" max="10000000000" step="1" value="${product.base_price}" required></label>
                    <label>${t('inventory')}<input name="stock" type="number" min="0" max="1000" step="1" value="${product.stock}" required></label>
                    <label class="listing-field-wide">Variantes (una por línea: Talla=M; Color=Azul | existencias | precio opcional)<textarea name="variants" rows="4" placeholder="Talla=M; Color=Azul | 8 | 85000">${escapeHtml(variantText(product.variants))}</textarea></label>
                    <label>${t('shippingCostUnit')}<input name="shipping_cost" type="number" min="0" max="1000000" step="1" value="${product.shipping_cost}" required></label>
                    <label>${t('shippingMinDays')}<input name="shipping_min_days" type="number" min="1" max="90" step="1" value="${product.shipping_min_days}" required></label>
                    <label>${t('shippingMaxDays')}<input name="shipping_max_days" type="number" min="1" max="90" step="1" value="${product.shipping_max_days}" required></label>
                    <label>${t('flashDiscount')}<select name="sale_percent">
                        ${[0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 70].map((percent) => `<option value="${percent}" ${product.flash_sale_percent === percent ? 'selected' : ''}>${percent ? `${percent}%` : t('noOffer')}</option>`).join('')}
                    </select></label>
                    <label>${t('duration')}<select name="sale_hours">
                        ${[1, 6, 12, 24, 48, 72].map((hours) => `<option value="${hours}" ${hours === 24 ? 'selected' : ''}>${hours} h</option>`).join('')}
                    </select></label>
                    <div class="listing-actions">
                        <button type="submit"><i class="fa-regular fa-floppy-disk"></i> ${t('saveListing')}</button>
                        <button class="unlist-button" type="button" data-remove="${escapeHtml(product.id)}"><i class="fa-regular fa-trash-can"></i> ${t('removeListing')}</button>
                    </div>
                </form>
            </article>`).join('');
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

    async function loadSellerPayments() {
        const data = await api('/api/seller/payments');
        const statusLabels = {
            scheduled: 'Retenido',
            pending: 'Pendiente',
            processing: 'En proceso',
            submitted: 'En conciliación',
            unknown: 'Requiere conciliación',
            paid: 'Pagado',
            pendiente_de_pago_al_emprendedor: 'Pendiente de pago por XrteShop',
            pagado_al_emprendedor: 'Pagado',
            failed: 'Fallido',
            not_applicable: 'No aplica',
        };
        byId('seller-payment-balance').textContent =
            `Saldo pendiente: ${currency.format(data.balance.pending_cop)} · Pagado: ${currency.format(data.balance.paid_cop)} · Reintegros por conciliar: ${currency.format(data.balance.recovery_due_cop)}`;
        byId('seller-payment-list').innerHTML = data.payments.length
            ? data.payments.map((payment) => `
                <article class="seller-payment-card">
                    <strong>Pedido #${payment.order_id}</strong>
                    <span>Venta: ${currency.format(payment.amount)}</span>
                    <span>Comisión 10%: ${currency.format(payment.commission_amount)}</span>
                    <span>Neto estimado: ${currency.format(payment.net_amount)}</span>
                    ${payment.recovery_owed ? `<span>Reintegro pendiente: ${currency.format(payment.recovery_owed)}</span>` : ''}
                    <span>Estado: ${escapeHtml(statusLabels[payment.transfer_status] || payment.transfer_status)}</span>
                    ${payment.transfer_completed_at ? `<span>Fecha de pago: ${escapeHtml(payment.transfer_completed_at.slice(0, 10))} · Referencia: ${escapeHtml(payment.provider_transfer_id || '')}</span>` : ''}
                </article>`).join('')
            : '<p class="empty-listings">Aún no tienes ventas registradas.</p>';
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
                    name: String(formData.get('name') || ''),
                    description: String(formData.get('description') || ''),
                    price: Number(formData.get('price')),
                    stock: Number(formData.get('stock')),
                    shipping_cost: Number(formData.get('shipping_cost')),
                    shipping_min_days: Number(formData.get('shipping_min_days')),
                    shipping_max_days: Number(formData.get('shipping_max_days')),
                    variants: String(formData.get('variants') || ''),
                    sale_percent: Number(formData.get('sale_percent')),
                    sale_hours: Number(formData.get('sale_hours')),
                }),
            });
            showToast(t('listingUpdated'));
            await loadListings();
        } catch (error) {
            showToast(error.message);
        } finally {
            button.disabled = false;
        }
    });

    byId('seller-products').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-remove]');
        if (!button) return;
        if (!window.confirm(t('confirmRemoveListing'))) return;
        button.disabled = true;
        try {
            await api(`/api/seller/products/${encodeURIComponent(button.dataset.remove)}`, { method: 'DELETE' });
            await loadListings();
            showToast(t('listingRemoved'));
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
        await Promise.all([loadListings(), loadSellerPayments(), loadSellerOrders(), loadSellerReturns()]);
    }).catch((error) => {
        byId('seller-gate').hidden = false;
        byId('seller-gate').querySelector('p').textContent = `${error.message} Comprueba que la aplicación Python esté en marcha.`;
    });
});