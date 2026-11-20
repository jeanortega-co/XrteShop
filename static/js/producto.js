document.addEventListener('DOMContentLoaded', async () => {
    let csrfToken = '';
    let currentUser = null;
    let product = null;
    let cart = { items: [], count: 0, total: 0 };
    let maxCanAdd = 0;
    let saleTimerInterval = null;
    let saleRefreshPending = false;
    let selectedVariantId = null;
    let isFavorite = false;
    const guestCartStorageKey = 'xrteshop_guest_cart_v1';
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

    function savedGuestCart() {
        try {
            const value = JSON.parse(localStorage.getItem(guestCartStorageKey) || '[]');
            return Array.isArray(value) ? value : [];
        } catch (error) {
            console.warn('No se pudo leer el carrito local de invitado:', error);
            return [];
        }
    }

    async function restoreGuestCart(savedItems) {
        let restored = await api('/api/cart');
        for (const item of savedItems) {
            if (!item || typeof item.product_id !== 'string' || !Number.isInteger(item.quantity)) continue;
            const body = { product_id: item.product_id, quantity: item.quantity };
            if (typeof item.variant_id === 'string' && item.variant_id) body.variant_id = item.variant_id;
            try {
                restored = await api('/api/cart', { method: 'POST', body: JSON.stringify(body) });
            } catch (error) {
                console.warn('No se pudo restaurar un producto guardado en el carrito:', error);
            }
        }
        return restored;
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
        const variants = product.variants || [];
        const variantPicker = byId('detail-variants');
        if (variants.length) {
            if (!variants.some((variant) => variant.id === selectedVariantId)) {
                selectedVariantId = (variants.find((variant) => variant.stock > 0) || variants[0]).id;
            }
            variantPicker.hidden = false;
            variantPicker.innerHTML = `<label for="variant-choice">${window.PemuI18n.language === 'en' ? 'Options' : 'Opciones'}</label><select id="variant-choice">${variants.map((variant) => {
                const label = `${variant.label} · ${variant.stock} ${window.PemuI18n.language === 'en' ? 'available' : 'disponibles'}`;
                return `<option value="${variant.id}" ${variant.id === selectedVariantId ? 'selected' : ''} ${variant.stock < 1 ? 'disabled' : ''}>${label}</option>`;
            }).join('')}</select>`;
            byId('variant-choice').addEventListener('change', (event) => {
                selectedVariantId = event.currentTarget.value;
                renderProduct();
            });
        } else {
            variantPicker.hidden = true;
            selectedVariantId = null;
        }
        const selectedVariant = variants.find((variant) => variant.id === selectedVariantId);
        const cartKey = selectedVariant ? `variant:${selectedVariant.id}` : product.id;
        const existing = cart.items.find((item) => item.id === cartKey);
        const availableStock = selectedVariant ? selectedVariant.stock : product.stock;
        const displayedPrice = selectedVariant?.price || product.base_price || product.price;
        const actualPrice = product.flash_sale_percent
            ? Math.max(1, displayedPrice * (100 - product.flash_sale_percent) / 100)
            : displayedPrice;
        maxCanAdd = Math.max(0, Math.min(20 - (existing?.quantity || 0), availableStock - (existing?.quantity || 0)));
        byId('detail-loading').hidden = true;
        byId('product-detail').hidden = false;
        document.title = `${product.name} | XrteShop`;
        const productImages = [...new Set(
            (product.image_urls || [product.image]).filter((image) => typeof image === 'string' && image),
        )];
        const mainImage = byId('detail-image');
        mainImage.src = imagePath(productImages[0] || product.image);
        mainImage.alt = product.name;
        const thumbnails = byId('detail-image-thumbnails');
        thumbnails.replaceChildren();
        thumbnails.hidden = productImages.length < 2;
        productImages.forEach((imageUrl, index) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'detail-image-thumbnail';
            button.setAttribute('aria-label', `Mostrar imagen ${index + 1} de ${product.name}`);
            button.setAttribute('aria-pressed', String(index === 0));
            const thumbnail = document.createElement('img');
            thumbnail.src = imagePath(imageUrl);
            thumbnail.alt = '';
            thumbnail.loading = 'lazy';
            button.append(thumbnail);
            button.addEventListener('click', () => {
                mainImage.src = imagePath(imageUrl);
                thumbnails.querySelectorAll('.detail-image-thumbnail').forEach((item) => {
                    item.setAttribute('aria-pressed', String(item === button));
                });
            });
            thumbnails.append(button);
        });
        byId('detail-badge').textContent = product.badge || '';
        byId('detail-badge').hidden = !product.badge;
        byId('detail-category').textContent = window.PemuI18n.category(product.category);
        byId('detail-fact-category').textContent = window.PemuI18n.category(product.category);
        byId('detail-name').textContent = product.name;
        byId('detail-seller').textContent = `${t('soldBy')} ${product.seller_name || 'XrteShop'}`;
        byId('detail-fact-seller').textContent = product.seller_name || 'XrteShop';
        byId('detail-description').textContent = product.description;
        byId('detail-price').textContent = currency.format(actualPrice);
        byId('detail-original-price').textContent = currency.format(displayedPrice);
        byId('detail-original-price').hidden = displayedPrice <= actualPrice;
        byId('detail-shipping-cost').textContent = product.shipping_cost
            ? currency.format(product.shipping_cost)
            : t('shippingFree');
        byId('detail-shipping-time').textContent = t('shippingDaysRange')
            .replace('{min}', product.shipping_min_days)
            .replace('{max}', product.shipping_max_days);
        byId('detail-supplier-disclaimer').hidden = product.origin !== 'proveedor';
        updateSaleCountdown();
        byId('detail-stock-text').textContent = availableStock > 0 ? `${availableStock} ${t('availableUnits')}` : t('soldOut');
        byId('detail-stock').classList.toggle('sold-out', availableStock === 0);
        byId('detail-purchase').hidden = maxCanAdd === 0;
        if (maxCanAdd === 0) {
            byId('detail-message').textContent = availableStock === 0 ? t('itemSoldOut') : t('bagMaximum');
        }
        byId('detail-cart-count').textContent = String(cart.count);
        byId('favorite-toggle').setAttribute('aria-pressed', String(isFavorite));
        byId('favorite-toggle').querySelector('i').className = isFavorite ? 'fa-solid fa-heart' : 'fa-regular fa-heart';
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

    async function addToCart(quantityToAdd) {
        const button = byId('detail-add');
        const message = byId('detail-message');
        const selectedVariant = (product.variants || []).find((variant) => variant.id === selectedVariantId);
        const cartKey = selectedVariant ? `variant:${selectedVariant.id}` : product.id;
        const existing = cart.items.find((item) => item.id === cartKey);
        const quantity = (existing?.quantity || 0) + quantityToAdd;
        button.disabled = true;
        message.classList.remove('error');
        message.textContent = '';
        try {
            const body = { product_id: product.id, quantity };
            if (selectedVariant) body.variant_id = selectedVariant.id;
            cart = await api('/api/cart', {
                method: 'POST',
                body: JSON.stringify(body),
            });
            if (currentUser) {
                localStorage.removeItem(guestCartStorageKey);
            } else {
                localStorage.setItem(guestCartStorageKey, JSON.stringify(
                    cart.items.map((item) => ({
                        product_id: item.product_id || item.id,
                        variant_id: item.variant_id || '',
                        quantity: item.quantity,
                    })),
                ));
            }
            window.trackStoreEvent?.('add_to_cart', {
                product_id: product.id,
                variant_id: selectedVariant?.id || undefined,
            });
            byId('detail-cart-count').textContent = String(cart.count);
            message.textContent = `${product.name} ${t('addedBag').toLowerCase()}.`;
            renderProduct();
            message.textContent = `${product.name} ${t('addedBag').toLowerCase()}.`;
        } catch (error) {
            message.classList.add('error');
            message.textContent = error.message;
            button.disabled = maxCanAdd === 0;
        }
    }

    byId('detail-add').addEventListener('click', () => addToCart(Number(byId('detail-quantity').value)));
    byId('favorite-toggle').addEventListener('click', async () => {
        if (!currentUser) {
            window.location.assign(`/cuenta?next=${encodeURIComponent(window.location.pathname)}`);
            return;
        }
        try {
            const result = await api(`/api/favorites/${encodeURIComponent(product.id)}`, {
                method: isFavorite ? 'DELETE' : 'POST',
            });
            isFavorite = result.favorite;
            renderProduct();
        } catch (error) {
            byId('detail-message').textContent = error.message;
            byId('detail-message').classList.add('error');
        }
    });

    async function loadReviews() {
        const result = await api(`/api/products/${encodeURIComponent(productId)}/reviews`);
        byId('reviews-section').hidden = false;
        byId('detail-rating-stars').textContent = `${'★'.repeat(Math.round(result.average))}${'☆'.repeat(5 - Math.round(result.average))}`;
        byId('detail-rating-summary').textContent = result.count
            ? `${result.average} · ${result.count} ${window.PemuI18n.language === 'en' ? 'reviews' : 'reseñas'}`
            : (window.PemuI18n.language === 'en' ? 'No reviews yet' : 'Aún sin reseñas');
        byId('reviews-average').textContent = result.count ? `${result.average} / 5 · ${result.count}` : '';
        const form = byId('review-form');
        form.hidden = !result.can_review || Boolean(result.existing_review);
        byId('review-eligibility').textContent = result.existing_review
            ? (window.PemuI18n.language === 'en' ? 'You have already reviewed this product.' : 'Ya dejaste tu reseña verificada.')
            : result.can_review
                ? (window.PemuI18n.language === 'en' ? 'Verified buyers can leave one review.' : 'Tu compra pagada te permite dejar una reseña verificada.')
                : (window.PemuI18n.language === 'en' ? 'Reviews are available to verified buyers after purchase.' : 'Las reseñas están habilitadas para compradores con una compra pagada.');
        if (result.existing_review) {
            byId('review-rating').value = String(result.existing_review.rating);
            byId('review-text').value = result.existing_review.review;
        }
        const container = byId('product-reviews');
        container.replaceChildren();
        result.reviews.forEach((review) => {
            const article = document.createElement('article');
            article.className = 'product-review';
            const title = document.createElement('strong');
            title.textContent = review.reviewer;
            const rating = document.createElement('span');
            rating.textContent = `${'★'.repeat(review.rating)}${'☆'.repeat(5 - review.rating)}`;
            const text = document.createElement('p');
            text.textContent = review.review;
            article.append(title, rating, text);
            container.append(article);
        });
    }

    byId('review-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const message = byId('review-message');
        const submit = event.currentTarget.querySelector('button[type="submit"]');
        submit.disabled = true;
        message.textContent = '';
        try {
            await api(`/api/products/${encodeURIComponent(productId)}/reviews`, {
                method: 'POST',
                body: JSON.stringify({
                    rating: byId('review-rating').value,
                    review: byId('review-text').value,
                }),
            });
            message.textContent = window.PemuI18n.language === 'en' ? 'Your verified review was published.' : 'Tu reseña verificada se publicó.';
            await loadReviews();
        } catch (error) {
            message.textContent = error.message;
        } finally {
            submit.disabled = false;
        }
    });
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
        const guestItems = savedGuestCart();
        const sessionData = await api('/api/auth/me');
        csrfToken = sessionData.csrf_token;
        currentUser = sessionData.user;
        let cartData = await api('/api/cart');
        if (currentUser) {
            localStorage.removeItem(guestCartStorageKey);
        } else if (guestItems.length) {
            cartData = await restoreGuestCart(guestItems);
        }
        const productData = await api(`/api/products/${encodeURIComponent(productId)}`);
        cart = cartData;
        product = productData.product;
        if (currentUser) {
            const favorites = await api('/api/favorites');
            isFavorite = favorites.product_ids.includes(productId);
        }
        renderProduct();
        try {
            await loadReviews();
        } catch (error) {
            byId('reviews-section').hidden = false;
            byId('review-eligibility').textContent = `No se pudieron cargar las reseñas: ${error.message}`;
        }
        const pendingQuantity = Number(new URLSearchParams(window.location.search).get('add'));
        if (currentUser && Number.isInteger(pendingQuantity) && pendingQuantity > 0 && pendingQuantity <= 20) {
            window.history.replaceState(null, '', window.location.pathname);
            await addToCart(pendingQuantity);
        }
    } catch (error) {
        showUnavailable(error.message.includes('404') ? 'Este producto ya no aparece en el catálogo.' : error.message);
    }
});