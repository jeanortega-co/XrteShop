document.addEventListener('DOMContentLoaded', () => {
    const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
    const supplierForm = document.getElementById('supplier-link-form');
    const supplierUrlInput = document.getElementById('supplier-url');
    const supplierStatus = document.getElementById('supplier-link-status');
    const supplierPreviewSection = document.getElementById('supplier-preview-section');
    const supplierPreview = document.getElementById('supplier-preview');
    const supplierNameInput = document.getElementById('supplier-edit-name');
    const supplierDescriptionInput = document.getElementById('supplier-edit-description');
    const supplierCategoryInput = document.getElementById('supplier-edit-category');
    const supplierManualStock = document.getElementById('supplier-manual-stock');
    const supplierPublishButton = document.getElementById('supplier-publish-button');
    const supplierSourceLink = document.getElementById('supplier-source-link');
    const mercadolibreAccountStatus = document.getElementById('mercadolibre-account-status');
    const mercadolibreOAuthNotice = document.getElementById('mercadolibre-oauth-notice');
    const mercadolibreConnectLink = document.getElementById('mercadolibre-connect-link');
    const mercadolibreDisconnectButton = document.getElementById('mercadolibre-disconnect-button');
    const currency = new Intl.NumberFormat('es-CO', {
        style: 'currency',
        currency: 'COP',
        maximumFractionDigits: 0,
    });
    let supplierProduct = null;
    let supplierUrl = '';

    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text !== undefined && text !== null) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    async function requestJson(url, options = {}) {
        const response = await fetch(url, {
            credentials: 'same-origin',
            ...options,
            headers: {
                ...(options.headers || {}),
                ...(options.method ? { 'X-CSRF-Token': csrfToken } : {}),
            },
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo completar la solicitud.');
        return data;
    }

    function safeImageUrl(value) {
        try {
            const url = new URL(value);
            return url.protocol === 'https:' ? url.href : '';
        } catch (_error) {
            return '';
        }
    }

    function safeProductUrl(value) {
        try {
            const url = new URL(value);
            return url.protocol === 'https:' &&
                ['mercadolibre.com.co', 'www.mercadolibre.com.co', 'articulo.mercadolibre.com.co']
                    .includes(url.hostname.toLowerCase())
                ? url.href
                : '';
        } catch (_error) {
            return '';
        }
    }

    async function loadMercadolibreAccount() {
        try {
            const data = await requestJson('/api/admin/mercadolibre/account');
            mercadolibreConnectLink.hidden = Boolean(data.connected);
            mercadolibreDisconnectButton.hidden = !data.connected;
            if (!data.connected) {
                mercadolibreAccountStatus.textContent = 'No hay una cuenta de Mercado Libre conectada.';
                return;
            }
            const expiresAt = new Date(data.token_expires_at);
            mercadolibreAccountStatus.textContent =
                `Conectada: ${data.nickname || 'Cuenta Mercado Libre'} · ` +
                `ID ${data.external_user_id} · token vigente hasta ${expiresAt.toLocaleString('es-CO')}.`;
        } catch (error) {
            mercadolibreAccountStatus.textContent = error.message;
        }
    }

    const oauthResult = new URLSearchParams(window.location.search).get('oauth');
    const oauthMessages = {
        not_configured: 'Falta configurar las credenciales OAuth, la URL de retorno o la clave de cifrado.',
        error: 'Mercado Libre no completó la autorización. Revisa la configuración y vuelve a intentarlo.',
        storage_error: 'La autorización se completó, pero no se pudieron guardar sus tokens.',
        invalid_state: 'La autorización expiró o ya fue utilizada. Intenta conectarte otra vez.',
        not_authorized: 'La cuenta que inició la autorización ya no tiene permisos de administrador.',
        cancelled: 'La autorización de Mercado Libre fue cancelada.',
    };
    if (Object.prototype.hasOwnProperty.call(oauthMessages, oauthResult)) {
        mercadolibreOAuthNotice.textContent = oauthMessages[oauthResult];
        mercadolibreOAuthNotice.hidden = false;
    }
    loadMercadolibreAccount();

    mercadolibreDisconnectButton.addEventListener('click', async () => {
        if (!window.confirm(
            '¿Borrar de XrteShop los tokens cifrados de la cuenta? ' +
            'Esto no revoca el permiso en Mercado Libre.',
        )) return;
        mercadolibreDisconnectButton.disabled = true;
        try {
            await requestJson('/api/admin/mercadolibre/disconnect', { method: 'POST' });
            await loadMercadolibreAccount();
            mercadolibreAccountStatus.textContent =
                'Se borraron los tokens locales. Si quieres revocar el permiso OAuth, hazlo también en Mercado Libre.';
        } catch (error) {
            mercadolibreAccountStatus.textContent = error.message;
        } finally {
            mercadolibreDisconnectButton.disabled = false;
        }
    });

    function supplierOriginalPrice(value, currencyCode) {
        if (value === null || value === undefined || value === '') return 'No disponible';
        try {
            return new Intl.NumberFormat('es-CO', {
                style: 'currency',
                currency: currencyCode || 'COP',
                maximumFractionDigits: 2,
            }).format(Number(value));
        } catch (_error) {
            return `${value} ${currencyCode || ''}`.trim();
        }
    }

    function manualStockFor(variantId) {
        const input = [...supplierManualStock.querySelectorAll('[data-supplier-stock]')]
            .find((item) => item.dataset.supplierStock === String(variantId));
        if (!input || input.value === '') return null;
        const value = Number(input.value);
        return Number.isSafeInteger(value) && value >= 0 ? value : null;
    }

    function renderManualStockFields(product) {
        supplierManualStock.replaceChildren();
        const variants = product.variants || [];
        variants.forEach((variant) => {
            if (variant.supplier_stock_reported !== null) return;
            const label = element(
                'label',
                `Existencias confirmadas manualmente · ${variant.name}`,
            );
            const input = element('input');
            input.type = 'number';
            input.min = '0';
            input.step = '1';
            input.required = true;
            input.inputMode = 'numeric';
            input.dataset.supplierStock = String(variant.supplier_variant_id);
            label.append(input);
            supplierManualStock.append(label);
        });
        if (!variants.length && product.supplier_stock_reported === null) {
            const label = element('label', 'Existencias confirmadas manualmente');
            const input = element('input');
            input.type = 'number';
            input.min = '0';
            input.step = '1';
            input.required = true;
            input.inputMode = 'numeric';
            input.dataset.supplierStock = 'product';
            label.append(input);
            supplierManualStock.append(label);
        }
    }

    function supplierPublicationReady() {
        const product = supplierProduct;
        if (!product || !supplierNameInput.value.trim()
            || !product.supplier_product_id || !product.image_urls?.length) return false;
        const categoryAllowed = [
            'Accesorios', 'Cuidado personal', 'Electrónica',
            'Hogar', 'Moda', 'Otros',
        ].includes(supplierCategoryInput.value);
        if (!categoryAllowed) return false;
        if (product.supplier_platform !== 'mercadolibre'
            || product.status !== 'active'
            || !['COP', 'USD'].includes(product.supplier_currency)) return false;
        const variants = product.variants || [];
        if (variants.length) {
            let available = 0;
            for (const variant of variants) {
                if (!variant.supplier_variant_id
                    || !variant.name
                    || !variant.supplier_original_price) return false;
                const stock = variant.supplier_stock_reported === null
                    ? manualStockFor(variant.supplier_variant_id)
                    : variant.supplier_stock_reported;
                if (stock === null) return false;
                available += stock;
            }
            return available > 0;
        }
        if (product.supplier_original_price === null) return false;
        const stock = product.supplier_stock_reported === null
            ? manualStockFor('product')
            : product.supplier_stock_reported;
        return stock !== null && stock > 0;
    }

    function updateSupplierPublishState() {
        supplierPublishButton.disabled = !supplierPublicationReady();
    }

    function renderSupplierPreview(data) {
        const product = data.product;
        supplierProduct = product;
        supplierPreview.replaceChildren();
        const summary = element('div', null, 'supplier-preview-summary');
        const gallery = element('div', null, 'supplier-preview-gallery');
        (product.image_urls || []).slice(0, 8).forEach((value, index) => {
            const imageUrl = safeImageUrl(value);
            if (!imageUrl) return;
            const image = element('img');
            image.src = imageUrl;
            image.alt = `Imagen ${index + 1} de ${product.name || 'producto'}`;
            gallery.append(image);
        });
        const details = element('div', null, 'supplier-details');
        const sourceCurrency = product.supplier_currency || 'COP';
        const originalPrice = product.supplier_original_price;
        const supplierCost = product.supplier_cost_cop;
        details.append(
            element('h3', product.name || 'Producto del proveedor'),
            element('p', `Origen: Mercado Libre Colombia · ID: ${product.supplier_product_id || 'No disponible'}`),
            element('p', `Precio original: ${supplierOriginalPrice(originalPrice, sourceCurrency)}`),
            element('p', `Costo convertido a COP: ${supplierCost === undefined ? 'No disponible' : currency.format(supplierCost)}`),
            element('p', `Precio de venta (+10%): ${product.suggested_price === undefined ? 'No disponible' : currency.format(product.suggested_price)}`),
            element('p', `Existencias reportadas: ${product.supplier_stock_reported === null ? 'No informadas' : product.supplier_stock_reported}`),
        );
        if (product.description) details.append(element('p', product.description.slice(0, 800)));
        summary.append(gallery, details);
        supplierPreview.append(summary);

        if (product.variants?.length) {
            const list = element('div', null, 'supplier-variant-list');
            const table = document.createElement('table');
            const head = document.createElement('thead');
            const row = document.createElement('tr');
            ['Variante', 'SKU', 'Stock', 'Precio original', 'Precio +10%'].forEach((label) => {
                row.append(element('th', label));
            });
            head.append(row);
            table.append(head);
            const body = document.createElement('tbody');
            product.variants.forEach((variant) => {
                const variantRow = document.createElement('tr');
                const stock = variant.supplier_stock_reported === null
                    ? 'Confirmación manual requerida'
                    : String(variant.supplier_stock_reported);
                [
                    variant.name || 'Sin nombre',
                    variant.sku || '—',
                    stock,
                    supplierOriginalPrice(variant.supplier_original_price, sourceCurrency),
                    variant.suggested_price === undefined
                        ? 'No disponible'
                        : currency.format(variant.suggested_price),
                ].forEach((value) => variantRow.append(element('td', value)));
                body.append(variantRow);
            });
            table.append(body);
            list.append(table);
            supplierPreview.append(list);
        }
        (product.blockers || []).forEach((blocker) => {
            supplierPreview.append(element('p', blocker, 'supplier-warning'));
        });
        (product.warnings || []).forEach((warning) => {
            supplierPreview.append(element('p', warning, 'supplier-warning'));
        });

        supplierNameInput.value = product.name || '';
        supplierDescriptionInput.value = product.description || '';
        supplierCategoryInput.value = [
            'Accesorios', 'Cuidado personal', 'Electrónica',
            'Hogar', 'Moda', 'Otros',
        ].includes(product.category) ? product.category : 'Otros';
        renderManualStockFields(product);
        supplierSourceLink.href = safeProductUrl(product.supplier_url) || '#';
        supplierSourceLink.hidden = supplierSourceLink.href === '#';
        supplierPreviewSection.hidden = false;
        updateSupplierPublishState();
    }

    supplierForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        supplierUrl = supplierUrlInput.value.trim();
        supplierProduct = null;
        supplierPublishButton.disabled = true;
        supplierPreviewSection.hidden = true;
        supplierStatus.textContent = 'Consultando la publicación o ID en Mercado Libre…';
        try {
            const data = await requestJson(
                `/api/admin/supplier/preview?url=${encodeURIComponent(supplierUrl)}`,
            );
            renderSupplierPreview(data);
            supplierStatus.textContent = 'Vista previa lista. Revisa y edita los datos antes de integrarla.';
        } catch (error) {
            supplierStatus.textContent = error.message;
        }
    });

    [supplierNameInput, supplierDescriptionInput, supplierCategoryInput].forEach((input) => {
        input.addEventListener('input', updateSupplierPublishState);
        input.addEventListener('change', updateSupplierPublishState);
    });
    supplierManualStock.addEventListener('input', updateSupplierPublishState);
    supplierUrlInput.addEventListener('input', () => {
        supplierProduct = null;
        supplierPreviewSection.hidden = true;
        supplierPublishButton.disabled = true;
    });

    supplierPublishButton.addEventListener('click', async () => {
        if (!supplierPublicationReady()) return;
        supplierPublishButton.disabled = true;
        supplierStatus.textContent = 'Volviendo a consultar la publicación e integrando…';
        const overrides = {
            name: supplierNameInput.value.trim(),
            description: supplierDescriptionInput.value,
            category: supplierCategoryInput.value,
            variant_stocks: {},
        };
        supplierManualStock.querySelectorAll('[data-supplier-stock]').forEach((input) => {
            if (input.dataset.supplierStock === 'product') {
                overrides.stock = Number(input.value);
            } else if (input.value !== '') {
                overrides.variant_stocks[input.dataset.supplierStock] = Number(input.value);
            }
        });
        try {
            const result = await requestJson('/api/admin/supplier/publish', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: supplierUrl, overrides }),
            });
            const storeLink = element('a', 'Abrir producto en la tienda');
            storeLink.href = `/producto/${encodeURIComponent(result.product_id)}`;
            storeLink.target = '_blank';
            storeLink.rel = 'noopener noreferrer';
            supplierStatus.replaceChildren(
                element(
                    'span',
                    `Producto integrado. Precio: ${currency.format(result.price)}; existencias: ${result.stock}. `,
                ),
                storeLink,
            );
        } catch (error) {
            supplierStatus.textContent = error.message;
        } finally {
            updateSupplierPublishState();
        }
    });

});
