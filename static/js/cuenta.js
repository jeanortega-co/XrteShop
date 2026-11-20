document.addEventListener('DOMContentLoaded', () => {
    let csrfToken = '';
    let mode = 'login';
    let forgotPasswordStage = 'email';
    let verificationState = 'none';
    let currentOrders = null;
    let currentRewards = null;
    let currency = new Intl.NumberFormat(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO', {
        style: 'currency', currency: 'COP', currencyDisplay: 'code', maximumFractionDigits: 0,
    });
    const byId = (id) => document.getElementById(id);
    const t = (key) => window.PemuI18n.t(key);
    const guestCartStorageKey = 'xrteshop_guest_cart_v1';

    function setupAccountCanvas() {
        const canvas = byId('account-canvas');
        const context = canvas.getContext('2d');
        if (!context) return;
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        let width = 0;
        let height = 0;

        function draw(timestamp) {
            context.clearRect(0, 0, width, height);
            context.lineWidth = 0.8;
            for (let line = 0; line < 20; line += 1) {
                context.beginPath();
                for (let x = -12; x <= width + 12; x += 8) {
                    const horizontal = x / Math.max(width, 1);
                    const movement = reducedMotion ? 0 : timestamp * 0.00016;
                    const wave = Math.sin(horizontal * 8 + line * 0.19 + movement) * 18
                        + Math.sin(horizontal * 15 - line * 0.12 - movement * 0.7) * 7;
                    const y = height * 0.58 + (line - 9.5) * 14 + wave;
                    if (x === -12) context.moveTo(x, y);
                    else context.lineTo(x, y);
                }
                context.strokeStyle = line % 7 === 0 ? 'rgba(230, 120, 93, 0.5)' : 'rgba(216, 238, 130, 0.38)';
                context.stroke();
            }
            if (!reducedMotion) window.requestAnimationFrame(draw);
        }

        function resizeCanvas() {
            const bounds = canvas.getBoundingClientRect();
            const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
            width = bounds.width;
            height = bounds.height;
            canvas.width = Math.round(width * pixelRatio);
            canvas.height = Math.round(height * pixelRatio);
            context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
            if (reducedMotion) draw(0);
        }

        if ('ResizeObserver' in window) new ResizeObserver(resizeCanvas).observe(canvas);
        else window.addEventListener('resize', resizeCanvas);
        resizeCanvas();
        if (!reducedMotion) window.requestAnimationFrame(draw);
    }

    setupAccountCanvas();

    async function fetchApi(path, options) {
        try {
            return await fetch(path, options);
        } catch (error) {
            if (error instanceof TypeError) {
                throw new Error('No se pudo conectar con XrteShop. Inicia el servidor con "python app.py" y abre http://127.0.0.1:5000/cuenta.');
            }
            throw error;
        }
    }

    async function readApiResponse(response) {
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
            throw new Error('El servidor no respondió a la API de XrteShop. Abre la tienda desde http://127.0.0.1:5000/cuenta.');
        }
        return response.json();
    }

    async function refreshCsrfToken() {
        const response = await fetchApi('/api/session', { credentials: 'same-origin', cache: 'no-store' });
        const data = await readApiResponse(response);
        if (!response.ok || !data.csrf_token) throw new Error('No se pudo validar la sesión. Recarga la página e inténtalo de nuevo.');
        csrfToken = data.csrf_token;
    }

    async function restoreGuestCart() {
        let cart = await api('/api/cart');
        let items = [];
        try {
            const saved = JSON.parse(localStorage.getItem(guestCartStorageKey) || '[]');
            if (Array.isArray(saved)) items = saved;
        } catch (error) {
            console.warn('No se pudo leer el carrito de invitado guardado:', error);
        }
        for (const item of items) {
            if (!item || typeof item.product_id !== 'string' || !Number.isInteger(item.quantity)) continue;
            const body = { product_id: item.product_id, quantity: item.quantity };
            if (typeof item.variant_id === 'string' && item.variant_id) body.variant_id = item.variant_id;
            try {
                cart = await api('/api/cart', { method: 'POST', body: JSON.stringify(body) });
            } catch (error) {
                console.warn('No se pudo restaurar un producto guardado en el carrito:', error);
            }
        }
        return cart;
    }

    async function api(path, options = {}) {
        const method = (options.method || 'GET').toUpperCase();
        const headers = { ...(options.headers || {}) };
        if (options.body) headers['Content-Type'] = 'application/json';
        if (method !== 'GET' && !csrfToken) await refreshCsrfToken();
        if (method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
        const requestOptions = { ...options, method, headers, credentials: 'same-origin' };
        let response = await fetchApi(path, requestOptions);
        if (method !== 'GET' && response.status === 403) {
            await refreshCsrfToken();
            headers['X-CSRF-Token'] = csrfToken;
            response = await fetchApi(path, requestOptions);
        }
        const data = await readApiResponse(response);
        if (!response.ok) {
            const error = new Error(data.error || 'No se pudo completar la solicitud.');
            error.status = response.status;
            throw error;
        }
        return data;
    }

    function getSafeNextPath() {
        const value = new URLSearchParams(window.location.search).get('next');
        if (!value || !value.startsWith('/') || value.startsWith('//')) return null;
        const destination = new URL(value, window.location.origin);
        if (destination.origin !== window.location.origin) return null;
        const isCheckout = destination.pathname === '/' && destination.searchParams.get('checkout') === '1';
        if (!isCheckout && destination.pathname !== '/emprendedores' && !/^\/producto\/[a-zA-Z0-9_-]+$/.test(destination.pathname)) return null;
        return `${destination.pathname}${destination.search}${destination.hash}`;
    }

    function returnToNextPath() {
        const nextPath = getSafeNextPath();
        if (!nextPath) return false;
        window.location.replace(nextPath);
        return true;
    }

    function setMode(nextMode) {
        mode = nextMode;
        const isRegister = mode === 'register';
        const isForgotPassword = mode === 'forgot-password';
        const isForgotPasswordEmail = isForgotPassword && forgotPasswordStage === 'email';
        const isForgotPasswordCode = isForgotPassword && forgotPasswordStage === 'code';
        const isVerifyEmail = mode === 'verify-email';
        byId('name-field').hidden = !isRegister;
        byId('confirm-password-field').hidden = !isRegister;
        byId('auth-confirm-password').required = isRegister;
        byId('remember-row').hidden = isRegister || isForgotPassword || isVerifyEmail;
        byId('auth-name').required = isRegister;
        byId('auth-password').autocomplete = isRegister ? 'new-password' : 'current-password';
        byId('auth-password').minLength = isRegister ? 8 : 0;
        byId('auth-password').hidden = isForgotPassword || isVerifyEmail;
        byId('auth-password').required = !(isForgotPassword || isVerifyEmail) && !isRegister ? true : isRegister;
        byId('auth-password').closest('.form-field').hidden = isForgotPassword || isVerifyEmail;
        byId('code-field').hidden = !isVerifyEmail && !isForgotPasswordCode;
        byId('new-password-field').hidden = !isForgotPasswordCode;
        byId('auth-code').required = isVerifyEmail || isForgotPasswordCode;
        byId('auth-new-password').required = isForgotPasswordCode;
        byId('resend-verification-button').hidden = !isVerifyEmail;
        byId('forgot-password-button').hidden = isRegister || isVerifyEmail;
        byId('forgot-password-button').textContent = isForgotPassword ? t('forgotLogin') : t('forgotPassword');
        byId('login-tab').classList.toggle('active', !isRegister);
        byId('login-tab').setAttribute('aria-selected', String(!isRegister));
        byId('register-tab').classList.toggle('active', isRegister);
        byId('register-tab').setAttribute('aria-selected', String(isRegister));
        byId('form-heading').textContent = isRegister ? t('registerHeading') : isForgotPassword ? t('recoverHeading') : isVerifyEmail ? t('verifyHeading') : t('loginHeading');
        byId('form-subheading').textContent = isRegister
            ? t('registerSubheading')
            : isForgotPasswordEmail
                ? t('forgottenEmailSubheading')
                : isForgotPasswordCode
                    ? t('forgottenCodeSubheading')
                    : isVerifyEmail
                        ? t('verifySubheading')
                        : t('loginSubheading');
        byId('submit-button').querySelector('span').textContent = isRegister
            ? t('createAccount')
            : isForgotPasswordEmail
                ? t('sendCode')
                : isForgotPasswordCode
                    ? t('resetPassword')
                    : isVerifyEmail
                        ? t('verifyAction')
                        : t('loginAction');
        byId('form-message').textContent = '';
        byId('form-message').classList.remove('success');
    }

    document.addEventListener('pemu-language-change', () => {
        currency = new Intl.NumberFormat(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO', {
            style: 'currency', currency: 'COP', currencyDisplay: 'code', maximumFractionDigits: 0,
        });
        setMode(mode);
        if (currentRewards) renderRewards(currentRewards);
        if (currentOrders) renderOrders(currentOrders);
    });

    function renderOrder(order) {
        const card = document.createElement('article');
        card.className = 'order-card';
        const top = document.createElement('div');
        top.className = 'order-topline';
        const number = document.createElement('strong');
        number.textContent = `${t('order')} #${order.id}`;
        const status = document.createElement('span');
        const orderStatusKeys = {
            Recibido: 'received', Pagado: 'paid', Preparando: 'preparing', Enviado: 'shipped',
            'En tránsito': 'inTransit', Entregado: 'delivered', Cancelado: 'cancelled',
        };
        status.textContent = t(orderStatusKeys[order.status] || order.status);
        top.append(number, status);
        const date = document.createElement('p');
        date.className = 'order-date';
        date.textContent = new Date(`${order.created_at.replace(' ', 'T')}Z`).toLocaleString(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO', { dateStyle: 'medium', timeStyle: 'short' });
        const items = document.createElement('div');
        items.className = 'order-items';
        items.textContent = order.items.map((item) => `${item.quantity} × ${item.product_name}`).join(' · ');
        const total = document.createElement('div');
        total.className = 'order-total';
        const label = document.createElement('span');
        label.textContent = t('total');
        const amount = document.createElement('strong');
        amount.textContent = currency.format(order.total);
        total.append(label, amount);
        card.append(top, date, items, total);
        const shipmentBySeller = new Map(order.shipments.map((shipment) => [String(shipment.seller_id), shipment]));
        order.shipments.forEach((shipment) => {
            const panel = document.createElement('div');
            panel.className = 'order-shipment';
            const statusKeys = {
                preparing: 'preparing',
                shipped: 'shipped',
                in_transit: 'inTransit',
                delivered: 'delivered',
            };
            const detail = [
                `${t('shipment')}: ${t(statusKeys[shipment.shipping_status] || shipment.shipping_status)}`,
                shipment.shipping_carrier ? `${t('carrier')}: ${shipment.shipping_carrier}` : '',
                shipment.tracking_number ? `${t('tracking')}: ${shipment.tracking_number}` : '',
                shipment.estimated_delivery_at ? `${t('estimatedDeliveryLabel')}: ${shipment.estimated_delivery_at}` : '',
            ].filter(Boolean).join(' · ');
            panel.textContent = detail;
            if (shipment.tracking_url) {
                const trackingLink = document.createElement('a');
                trackingLink.href = shipment.tracking_url;
                trackingLink.target = '_blank';
                trackingLink.rel = 'noopener noreferrer';
                trackingLink.textContent = window.PemuI18n.language === 'en'
                    ? 'Open carrier tracking'
                    : 'Abrir seguimiento de la transportadora';
                panel.append(' · ', trackingLink);
            }
            card.append(panel);
        });
        const existingReturns = new Map(order.returns.map((returnRequest) => [returnRequest.product_id, returnRequest]));
        order.items.forEach((item) => {
            const shipment = shipmentBySeller.get(String(item.seller_id));
            const returnRequest = existingReturns.get(item.product_id);
            if (returnRequest) {
                const state = document.createElement('p');
                state.className = 'order-date';
                state.textContent = `${t('returnLabel')}: ${returnRequest.status === 'received' ? t('returnRequestReceived') : returnRequest.status === 'reviewing' ? t('underReview') : returnRequest.status === 'accepted' ? t('refundSeparate') : t('returnRejected')}${returnRequest.free_return ? ` · ${t('freeReturnPending')}` : ''}`;
                card.append(state);
                return;
            }
            if (order.payment_status !== 'approved' || shipment?.shipping_status !== 'delivered' || !shipment.delivered_at) return;
            const deliveredTime = new Date(shipment.delivered_at).getTime();
            if (!Number.isFinite(deliveredTime) || Date.now() - deliveredTime > 90 * 24 * 60 * 60 * 1000) return;
            const form = document.createElement('form');
            form.className = 'return-request-form';
            form.dataset.orderReturn = String(order.id);
            form.dataset.product = item.product_id;
            const label = document.createElement('label');
            label.textContent = `${t('requestReturn')}: ${item.product_name}`;
            const reason = document.createElement('select');
            reason.name = 'reason';
            reason.setAttribute('aria-label', t('returnReason'));
            reason.required = true;
            reason.innerHTML = `<option value="">${t('chooseReason')}</option><option value="damaged">${t('arrivedDamaged')}</option><option value="incomplete">${t('incompleteOrder')}</option><option value="not_as_described">${t('notAsDescribed')}</option><option value="other">${t('otherReason')}</option>`;
            const details = document.createElement('textarea');
            details.name = 'details';
            details.maxLength = 1000;
            details.placeholder = t('whatHappened');
            const button = document.createElement('button');
            button.type = 'submit';
            button.textContent = t('sendRequest');
            form.append(label, reason, details, button);
            card.append(form);
        });
        return card;
    }

    function fillProfile(profile) {
        const fields = {
            name: 'profile-name',
            phone: 'profile-phone',
            address: 'profile-address',
            department: 'profile-department',
            city: 'profile-city',
            postal_code: 'profile-postal-code',
            delivery_notes: 'profile-delivery-notes',
            preferred_payment_method: 'profile-payment-method',
            buyer_nequi_phone: 'profile-nequi-phone',
        };
        Object.entries(fields).forEach(([key, id]) => { byId(id).value = profile[key] || (key === 'preferred_payment_method' ? 'auto' : ''); });
        byId('profile-personalization').checked = Boolean(profile.personalization_enabled);
        byId('account-name').textContent = profile.name;
    }

    function renderRewards(rewards) {
        const button = byId('spin-reward');
        button.disabled = !rewards.can_spin;
        button.querySelector('span').textContent = rewards.can_spin ? t('spinCoupon') : t('spinUnavailable');
        const points = Number(rewards.points_balance) || 0;
        byId('loyalty-points').textContent = points.toLocaleString('es-CO');
        const tierKey = ({ bronce: 'tierBronze', plata: 'tierSilver', oro: 'tierGold' })[rewards.loyalty_tier] || 'tierBronze';
        byId('loyalty-tier').textContent = `${t('loyaltyTier')} ${t(tierKey)}`;
        byId('loyalty-progress').textContent = points >= 100
            ? t('pointsCanRedeem')
            : t('pointsUntilCoupon').replace('{points}', String(100 - points));
        const redeemPoints = byId('redeem-points');
        Array.from(redeemPoints.options).forEach((option) => {
            option.disabled = Number(option.value) > points;
        });
        byId('redeem-points-form').querySelector('[type="submit"]').disabled = points < 100;
        const list = byId('coupon-list');
        list.replaceChildren();
        rewards.coupons.forEach((coupon) => {
            const card = document.createElement('div');
            card.className = 'coupon-card';
            const copy = document.createElement('div');
            const code = document.createElement('strong');
            code.textContent = coupon.code;
            const expiry = document.createElement('span');
            expiry.textContent = ` ${coupon.discount_percent}% · ${t('expires')} ${new Date(coupon.expires_at).toLocaleDateString(window.PemuI18n.language === 'en' ? 'en-CO' : 'es-CO')}`;
            const copyButton = document.createElement('button');
            copyButton.type = 'button';
            copyButton.dataset.copyCoupon = coupon.code;
            copyButton.textContent = t('copyCoupon');
            copy.append(code, document.createElement('br'), expiry);
            card.append(copy, copyButton);
            list.append(card);
        });
    }

    async function loadRewards() {
        currentRewards = await api('/api/rewards');
        renderRewards(currentRewards);
    }

    function renderOrders(orders) {
        byId('orders-count').textContent = `${orders.length} ${orders.length === 1 ? t('order') : t('ordersCount')}`;
        const list = byId('orders-list');
        list.replaceChildren();
        if (!orders.length) {
            const empty = document.createElement('p');
            empty.className = 'no-orders';
            empty.textContent = t('noOrders');
            list.append(empty);
        } else {
            orders.forEach((order) => list.append(renderOrder(order)));
        }
    }

    async function showAccount(user) {
        byId('session-loading').hidden = true;
        byId('auth-panel').hidden = true;
        byId('account-panel').hidden = false;
        byId('account-name').textContent = user.name;
        byId('account-email').textContent = user.email;
        const list = byId('orders-list');
        currentOrders = null;
        list.innerHTML = `<p class="no-orders">${t('orderLoading')}</p>`;
        try {
            const data = await api('/api/account/profile');
            fillProfile(data.profile);
            await loadRewards();
        } catch (error) {
            byId('profile-message').textContent = error.message;
        }
        try {
            const data = await api('/api/orders');
            currentOrders = data.orders;
            renderOrders(currentOrders);
        } catch (error) {
            list.textContent = error.message;
        }
    }

    byId('profile-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = byId('profile-message');
        const button = form.querySelector('button[type="submit"]');
        message.textContent = '';
        message.classList.remove('success');
        button.disabled = true;
        try {
            const values = Object.fromEntries(new FormData(form));
            values.personalization_enabled = byId('profile-personalization').checked;
            const profile = await api('/api/account/profile', {
                method: 'PUT',
                body: JSON.stringify(values),
            });
            fillProfile(profile.profile);
            message.textContent = 'Tus datos quedaron guardados.';
            message.classList.add('success');
            await loadRewards();
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    byId('spin-reward').addEventListener('click', async (event) => {
        const button = event.currentTarget;
        const message = byId('rewards-message');
        button.disabled = true;
        button.classList.add('spinning');
        message.textContent = '';
        message.classList.remove('success');
        try {
            const result = await api('/api/rewards/spin', { method: 'POST', body: '{}' });
            message.textContent = `${result.message} Código: ${result.coupon.code}`;
            message.classList.add('success');
            await loadRewards();
        } catch (error) {
            message.textContent = error.message;
            button.disabled = false;
        } finally {
            button.classList.remove('spinning');
        }
    });

    byId('redeem-points-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const button = form.querySelector('[type="submit"]');
        const message = byId('rewards-message');
        button.disabled = true;
        message.textContent = '';
        message.classList.remove('success');
        try {
            const result = await api('/api/rewards/redeem', {
                method: 'POST',
                body: JSON.stringify({ points: Number(byId('redeem-points').value) }),
            });
            message.textContent = `${result.message} Código: ${result.coupon.code}`;
            message.classList.add('success');
            await loadRewards();
        } catch (error) {
            message.textContent = error.message;
            await loadRewards();
        }
    });

    byId('coupon-list').addEventListener('click', async (event) => {
        const button = event.target.closest('[data-copy-coupon]');
        if (!button) return;
        try {
            await navigator.clipboard.writeText(button.dataset.copyCoupon);
            button.textContent = 'Copiado';
        } catch (error) {
            byId('rewards-message').textContent = 'No se pudo copiar automáticamente. Selecciona y copia el código del cupón.';
        }
    });

    byId('orders-list').addEventListener('submit', async (event) => {
        const form = event.target.closest('[data-order-return]');
        if (!form) return;
        event.preventDefault();
        const button = form.querySelector('button[type="submit"]');
        button.disabled = true;
        try {
            const result = await api(`/api/orders/${form.dataset.orderReturn}/returns`, {
                method: 'POST',
                body: JSON.stringify({
                    product_id: form.dataset.product,
                    reason: new FormData(form).get('reason'),
                    details: new FormData(form).get('details'),
                }),
            });
            const status = document.createElement('p');
            status.className = 'order-date';
            status.textContent = `${result.message}${result.free_return ? ' La primera devolución del pedido es sin costo; XrteShop coordinará los siguientes pasos.' : ''}`;
            form.replaceWith(status);
        } catch (error) {
            const status = document.createElement('p');
            status.className = 'form-message';
            status.textContent = error.message;
            form.prepend(status);
            button.disabled = false;
        }
    });

    document.querySelectorAll('[data-mode]').forEach((button) => button.addEventListener('click', () => setMode(button.dataset.mode)));
    document.addEventListener('click', (event) => {
        const toggle = event.target.closest('[data-password-toggle]');
        if (!toggle) return;
        const input = byId(toggle.dataset.passwordToggle);
        const visible = input.type === 'password';
        input.type = visible ? 'text' : 'password';
        toggle.setAttribute('aria-label', t(visible ? 'hidePassword' : 'showPassword'));
        toggle.innerHTML = `<i class="fa-regular ${visible ? 'fa-eye-slash' : 'fa-eye'}" aria-hidden="true"></i>`;
    });

    byId('forgot-password-button').addEventListener('click', async () => {
        if (mode === 'forgot-password' && forgotPasswordStage === 'code') {
            forgotPasswordStage = 'email';
            setMode('login');
            return;
        }
        forgotPasswordStage = 'email';
        setMode('forgot-password');
        byId('auth-code').value = '';
        byId('auth-new-password').value = '';
        byId('form-message').textContent = 'Introduce tu correo electrónico.';
    });

    byId('resend-verification-button').addEventListener('click', async () => {
        const email = byId('auth-email').value.trim();
        if (!email) {
            byId('form-message').textContent = 'Escribe tu correo para reenviar el código.';
            return;
        }
        try {
            await api('/api/resend-verification', { method: 'POST', body: JSON.stringify({ email }) });
            byId('form-message').textContent = 'Se reenvió el código de verificación.';
            byId('form-message').classList.add('success');
        } catch (error) {
            byId('form-message').textContent = error.message;
        }
    });

    byId('auth-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = byId('form-message');
        const button = byId('submit-button');
        const data = Object.fromEntries(new FormData(form));
        if (mode === 'login') data.remember_me = byId('remember-me').checked;
        message.textContent = '';
        message.classList.remove('success');
        button.disabled = true;
        if (mode === 'register') {
            const password = data.password || '';
            if (password.length < 8) {
                message.textContent = t('passwordRequirementsMessage');
                button.disabled = false;
                return;
            }
            if (password !== data.confirm_password) {
                message.textContent = t('passwordsDoNotMatch');
                button.disabled = false;
                return;
            }
        }
        try {
            button.querySelector('span').textContent = t('authWorking');
            if (mode === 'forgot-password') {
                if (forgotPasswordStage === 'email') {
                    if (!data.email) {
                        throw new Error('Introduce tu correo electrónico para continuar.');
                    }
                    const response = await api('/api/forgot-password', { method: 'POST', body: JSON.stringify({ email: data.email }) });
                    forgotPasswordStage = 'code';
                    setMode('forgot-password');
                    byId('auth-email').value = data.email;
                    byId('auth-code').value = '';
                    byId('auth-new-password').value = '';
                    message.textContent = response.message || 'Te enviamos un código a tu correo.';
                    message.classList.add('success');
                    return;
                }
                if (!data.email || !data.code || !data.new_password) {
                    throw new Error('Completa el correo, el código y la nueva contraseña.');
                }
                const response = await api('/api/reset-password', { method: 'POST', body: JSON.stringify(data) });
                message.textContent = response.message || 'Contraseña actualizada.';
                message.classList.add('success');
                forgotPasswordStage = 'email';
                setMode('login');
                byId('auth-email').value = data.email;
                byId('auth-password').value = '';
                return;
            }
            if (mode === 'verify-email') {
                if (!data.email || !data.code) {
                    throw new Error('Completa el correo y el código de verificación.');
                }
                const response = await api('/api/verify-email', { method: 'POST', body: JSON.stringify(data) });
                message.textContent = response.message || 'Correo verificado.';
                message.classList.add('success');
                setMode('login');
                byId('auth-email').value = data.email;
                return;
            }
            const authEndpoint = mode === 'register' ? '/api/auth/register' : '/api/auth/login';
            const response = await api(authEndpoint, { method: 'POST', body: JSON.stringify(data) });
            if (response.verification_required) {
                verificationState = 'pending';
                setMode('verify-email');
                byId('auth-email').value = response.email;
                message.textContent = response.message;
                message.classList.add('success');
                return;
            }
            if (response.redirect_to === '/admin') {
                window.location.replace('/admin');
                return;
            }
            csrfToken = (await api('/api/session')).csrf_token;
            if (returnToNextPath()) return;
            await showAccount(response.user);
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
            button.querySelector('span').textContent = mode === 'register'
                ? t('createAccount')
                : mode === 'forgot-password'
                    ? forgotPasswordStage === 'email' ? t('sendCode') : t('resetPassword')
                    : mode === 'verify-email' ? t('verifyAction') : t('login');
        }
    });

    byId('logout-button').addEventListener('click', async () => {
        try {
            await api('/api/auth/logout', { method: 'POST', body: '{}' });
            csrfToken = (await api('/api/session')).csrf_token;
            byId('session-loading').hidden = true;
            byId('account-panel').hidden = true;
            byId('auth-panel').hidden = false;
            byId('auth-form').reset();
            setMode('login');
        } catch (error) {
            byId('form-message').textContent = error.message;
        }
    });

    const checkoutDestination = getSafeNextPath();
    if (checkoutDestination && new URL(checkoutDestination, window.location.origin).searchParams.get('checkout') === '1') {
        byId('checkout-auth-notice').hidden = false;
    }

    api('/api/auth/me').then(async (data) => {
        csrfToken = data.csrf_token;
        if (data.user) {
            localStorage.removeItem(guestCartStorageKey);
            if (!returnToNextPath()) showAccount(data.user);
        } else {
            await restoreGuestCart();
            byId('session-loading').hidden = true;
            byId('auth-panel').hidden = false;
        }
    }).catch((error) => {
        byId('session-loading').hidden = true;
        byId('auth-panel').hidden = false;
        byId('form-message').textContent = `${error.message} Comprueba que la aplicación Python esté en marcha.`;
    });

    byId('auth-email').addEventListener('input', () => {
        if (mode === 'forgot-password' && forgotPasswordStage === 'email') {
            byId('form-message').textContent = 'Introduce tu correo electrónico.';
            byId('form-message').classList.remove('success');
        }
    });
});