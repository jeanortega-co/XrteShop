document.addEventListener('DOMContentLoaded', async () => {
    const status = document.getElementById('admin-status');
    const currency = new Intl.NumberFormat('es-CO', {
        style: 'currency',
        currency: 'COP',
        maximumFractionDigits: 0,
    });
    let csrfToken = '';
    let catalogCleanupSnapshot = null;

    async function sendJson(path, method, body) {
        const response = await fetch(path, {
            method,
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken,
            },
            body: JSON.stringify(body),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo completar la operación.');
        return data;
    }

    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function renderPayouts(items) {
        const container = document.getElementById('admin-payment-payouts');
        container.replaceChildren();
        if (!items.length) {
            container.append(element('p', 'No hay pagos pendientes a emprendedores.', 'admin-empty'));
            return;
        }
        items.forEach((item) => {
            const row = element('article', null, 'admin-row admin-operation-row');
            row.append(element('strong', `Pedido #${item.order_id} · ${item.seller_name || `Vendedor ${item.seller_id}`}`));
            row.append(element('small', `${item.transfer_status} · Neto a pagar ${currency.format(item.net_amount)} · Comisión XrteShop ${currency.format(item.commission_amount)}`));
            const date = element('input');
            date.type = 'date';
            const today = new Date();
            date.value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
            date.setAttribute('aria-label', `Fecha del pago al emprendedor para el pedido ${item.order_id}`);
            const reference = element('input');
            reference.maxLength = 100;
            reference.placeholder = 'Referencia de transferencia';
            reference.setAttribute('aria-label', `Referencia del pago al emprendedor para el pedido ${item.order_id}`);
            const button = element('button', 'Marcar pago realizado');
            button.type = 'button';
            button.addEventListener('click', async () => {
                if (!reference.value.trim()) {
                    status.textContent = 'Ingresa una referencia verificable del pago.';
                    return;
                }
                if (!window.confirm(`¿Confirmas que transferiste ${currency.format(item.net_amount)} al emprendedor?`)) return;
                button.disabled = true;
                try {
                    await sendJson(`/api/admin/payouts/${encodeURIComponent(item.split_id)}/mark-paid`, 'POST', {
                        payment_date: date.value,
                        payment_reference: reference.value.trim(),
                    });
                    window.location.reload();
                } catch (error) {
                    status.textContent = error.message;
                    button.disabled = false;
                }
            });
            row.append(date, reference, button);
            container.append(row);
        });
    }

    function renderDisputes(items) {
        const container = document.getElementById('admin-payment-disputes');
        container.replaceChildren();
        if (!items.length) {
            container.append(element('p', 'No hay disputas registradas.', 'admin-empty'));
            return;
        }
        items.forEach((item) => {
            const row = element('article', null, 'admin-row admin-operation-row');
            row.append(element('strong', `Disputa ${item.provider_dispute_id} · pedido #${item.order_id}`));
            row.append(element('small', `${currency.format(item.amount)} · ${item.status} · pago ${item.payment_id || 'sin ID'}`));
            const select = element('select');
            [['open', 'Abierta'], ['under_review', 'En revisión'], ['won', 'Ganada por XrteShop'], ['lost', 'Perdida']].forEach(([value, label]) => {
                const option = element('option', label);
                option.value = value;
                option.selected = value === item.status;
                select.append(option);
            });
            const button = element('button', 'Actualizar estado');
            button.type = 'button';
            button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    await sendJson(`/api/admin/disputes/${item.id}`, 'PATCH', { status: select.value });
                    window.location.reload();
                } catch (error) {
                    status.textContent = error.message;
                    button.disabled = false;
                }
            });
            row.append(select, button);
            container.append(row);
        });
    }

    function renderRefundLiabilities(items) {
        const container = document.getElementById('admin-refund-liabilities');
        container.replaceChildren();
        const openItems = items.filter((item) => item.status === 'open');
        if (!openItems.length) {
            container.append(element('p', 'No hay reintegros pendientes.', 'admin-empty'));
            return;
        }
        openItems.forEach((item) => {
            const row = element('article', null, 'admin-row admin-operation-row');
            row.append(element('strong', `Pedido #${item.order_id} · ${item.seller_name}`));
            row.append(element('small', `Debe reintegrar ${currency.format(item.amount)} · registrado ${item.created_at}`));
            const input = element('input');
            input.maxLength = 100;
            input.placeholder = 'Referencia del reintegro verificado';
            input.setAttribute('aria-label', `Referencia de reintegro del vendedor ${item.seller_name}`);
            const button = element('button', 'Marcar conciliado');
            button.type = 'button';
            button.addEventListener('click', async () => {
                if (!input.value.trim()) {
                    status.textContent = 'Ingresa una referencia comprobable del reintegro.';
                    return;
                }
                button.disabled = true;
                try {
                    await sendJson(`/api/admin/refund-liabilities/${item.id}`, 'PATCH', {
                        settlement_reference: input.value.trim(),
                    });
                    window.location.reload();
                } catch (error) {
                    status.textContent = error.message;
                    button.disabled = false;
                }
            });
            row.append(input, button);
            container.append(row);
        });
    }

    function renderRows(container, items, title, detail) {
        container.replaceChildren();
        if (!items.length) {
            const empty = document.createElement('p');
            empty.className = 'admin-empty';
            empty.textContent = 'No hay registros para mostrar.';
            container.append(empty);
            return;
        }
        items.forEach((item) => {
            const row = document.createElement('article');
            row.className = 'admin-row';
            const heading = document.createElement('strong');
            heading.textContent = title(item);
            const info = document.createElement('small');
            info.textContent = detail(item);
            row.append(heading, info);
            container.append(row);
        });
    }

    function renderSupplierProducts(items) {
        const container = document.getElementById('admin-supplier-products');
        container.replaceChildren();
        if (!items.length) {
            container.append(element('p', 'No hay productos de proveedores importados.', 'admin-empty'));
            return;
        }
        items.forEach((item) => {
            const row = element('article', null, 'admin-row admin-operation-row');
            row.append(element('strong', `${item.name} · ${item.supplier_name}`));
            if (/^https:\/\/(?:www\.)?(?:mercadolibre\.com\.co|articulo\.mercadolibre\.com\.co)\//i.test(item.supplier_url || '')) {
                const sourceLink = element('a', 'Abrir producto original');
                sourceLink.href = item.supplier_url;
                sourceLink.target = '_blank';
                sourceLink.rel = 'noopener noreferrer';
                row.append(sourceLink);
            }
            const gatewayFee = item.gateway_fee_estimate === null
                ? 'por configurar'
                : currency.format(item.gateway_fee_estimate);
            const netProfit = item.net_profit_estimate === null
                ? 'no disponible hasta configurar la comisión'
                : currency.format(item.net_profit_estimate);
            row.append(element(
                'small',
                `Estado ${item.product_status} · Costo ${currency.format(item.supplier_cost)} · ` +
                `Precio ${currency.format(item.price)} · Comisión pasarela ${gatewayFee} · ` +
                `Flete estimado ${currency.format(item.supplier_shipping_estimate)} · ` +
                `Ganancia neta estimada ${netProfit} · Stock local ${item.stock} · ` +
                `Stock total reportado por el proveedor ${item.supplier_stock} · ${item.variants.length} variantes`,
            ));
            item.variants.forEach((variant) => {
                row.append(element(
                    'small',
                    `${variant.label} · precio ${currency.format(variant.price)} · ` +
                    `stock por variante reportado por proveedor ${variant.supplier_stock} · ` +
                    `stock publicado ${variant.stock}`,
                ));
            });
            if (item.supplier_stock > 0 && (
                !item.active || item.stock < Math.min(item.supplier_stock, 1000)
            )) {
                const form = element('form', null, 'admin-action-form');
                const variantInputs = [];
                if (item.variants.length) {
                    item.variants.forEach((variant) => {
                        const variantMaximum = Math.min(
                            variant.supplier_stock || item.supplier_stock,
                            1000,
                        );
                        const label = element(
                            'label',
                            `${variant.label} · máximo individual ${variantMaximum} ` +
                            `(total reportado ${Math.min(item.supplier_stock, 1000)})`,
                        );
                        const input = element('input');
                        input.type = 'number';
                        input.name = `variant-${variant.id}`;
                        input.min = '0';
                        input.max = String(variantMaximum);
                        input.step = '1';
                        input.required = true;
                        if (item.active) input.value = String(variant.stock);
                        label.append(input);
                        form.append(label);
                        variantInputs.push({ id: variant.id, input });
                    });
                } else {
                    const label = element(
                        'label',
                        `Stock a publicar (máximo ${Math.min(item.supplier_stock, 1000)})`,
                    );
                    const input = element('input');
                    input.type = 'number';
                    input.name = 'stock';
                    input.min = '1';
                    input.max = String(Math.min(item.supplier_stock, 1000));
                    input.step = '1';
                    input.required = true;
                    if (item.active) input.value = String(item.stock);
                    label.append(input);
                    form.append(label);
                }
                const shippingCost = element('label', 'Envío cobrado al comprador (COP)');
                const shippingCostInput = element('input');
                shippingCostInput.type = 'number';
                shippingCostInput.name = 'shipping_cost';
                shippingCostInput.min = '0';
                shippingCostInput.max = '1000000';
                shippingCostInput.step = '1';
                shippingCostInput.required = true;
                if (item.active) shippingCostInput.value = String(item.shipping_cost);
                shippingCost.append(shippingCostInput);
                form.append(shippingCost);
                const supplierShipping = element('label', 'Envío cotizado por proveedor (COP)');
                const supplierShippingInput = element('input');
                supplierShippingInput.type = 'number';
                supplierShippingInput.name = 'supplier_shipping_estimate';
                supplierShippingInput.min = '0';
                supplierShippingInput.max = '10000000';
                supplierShippingInput.step = '1';
                supplierShippingInput.required = true;
                if (item.active) {
                    supplierShippingInput.value = String(item.supplier_shipping_estimate);
                }
                supplierShipping.append(supplierShippingInput);
                form.append(supplierShipping);
                [
                    ['shipping_min_days', 'Entrega mínima (días)'],
                    ['shipping_max_days', 'Entrega máxima (días)'],
                ].forEach(([name, text]) => {
                    const label = element('label', text);
                    const input = element('input');
                    input.type = 'number';
                    input.name = name;
                    input.min = '1';
                    input.max = '90';
                    input.step = '1';
                    input.required = true;
                    label.append(input);
                    form.append(label);
                });
                [
                    ['inventory_verified', 'Verifiqué con el proveedor las existencias de cada variante que publicaré.'],
                    ['shipping_verified', 'Verifiqué costo, ruta y plazo de envío a Colombia.'],
                    ['fulfillment_verified', 'Puedo comprar y gestionar manualmente estos pedidos con el proveedor.'],
                ].forEach(([name, text]) => {
                    const label = element('label', text);
                    const input = element('input');
                    input.type = 'checkbox';
                    input.name = name;
                    input.required = true;
                    label.prepend(input);
                    form.append(label);
                });
                const approve = element(
                    'button',
                    item.active ? 'Actualizar stock validado' : 'Publicar tras validar',
                );
                approve.type = 'submit';
                form.append(approve);
                form.addEventListener('submit', async (event) => {
                    event.preventDefault();
                    approve.disabled = true;
                    const payload = {
                        shipping_cost: Number(shippingCostInput.value),
                        supplier_shipping_estimate: Number(supplierShippingInput.value),
                        shipping_min_days: Number(form.elements.shipping_min_days.value),
                        shipping_max_days: Number(form.elements.shipping_max_days.value),
                        inventory_verified: form.elements.inventory_verified.checked,
                        shipping_verified: form.elements.shipping_verified.checked,
                        fulfillment_verified: form.elements.fulfillment_verified.checked,
                    };
                    if (variantInputs.length) {
                        payload.variants = variantInputs.map(({ id, input }) => ({
                            id,
                            stock: Number(input.value),
                        }));
                    } else {
                        payload.stock = Number(form.elements.stock.value);
                    }
                    try {
                        await sendJson(
                            `/api/admin/suppliers/cj/products/${encodeURIComponent(item.id)}/approve`,
                            'POST',
                            payload,
                        );
                        window.location.reload();
                    } catch (error) {
                        status.textContent = error.message;
                        approve.disabled = false;
                    }
                });
                row.append(form);
            }
            container.append(row);
        });
    }

    function addMetric(container, label, value) {
        const card = document.createElement('article');
        card.className = 'admin-metric';
        const name = document.createElement('small');
        name.textContent = label;
        const amount = document.createElement('strong');
        amount.textContent = value;
        card.append(name, amount);
        container.append(card);
    }

    function renderFinance(data) {
        const metrics = document.getElementById('admin-finance-metrics');
        metrics.replaceChildren();
        [
            ['Cobrado hoy', data.approved_today],
            ['Cobrado esta semana', data.approved_week],
            ['Cobrado este mes', data.approved_month],
            ['Cobrado neto menos reembolsos', data.net_collected],
            ['Comisión XrteShop', data.marketplace_commission],
            ['Ganancia de importados', data.imported_product_profit],
            ['Pasarela estimada', data.gateway_fee_estimate],
            ['Ganancia neta estimada', data.net_business_profit],
            ['Pendiente a emprendedores', data.entrepreneur_payables_pending],
            ['Reembolsado', data.refunded],
        ].forEach(([label, amount]) => addMetric(metrics, label, currency.format(amount)));
        document.getElementById('admin-finance-note').textContent =
            `${data.settlement_note} ${data.gateway_fee_note}`;
        renderRows(
            document.getElementById('admin-finance-methods'),
            data.collected_by_method,
            (item) => item.payment_method || 'Método no informado',
            (item) => `${currency.format(item.amount_cop)} · aprobado por webhook`,
        );
        renderRows(
            document.getElementById('admin-reconciliation-list'),
            data.reconciliations,
            (item) => `${item.provider_reference} · ${item.provider_status || 'sin estado'}`,
            (item) => `Reportado ${currency.format(item.provider_amount_cop)} · ` +
                `libro ${item.local_amount_cop === null ? 'sin coincidencia' : currency.format(item.local_amount_cop)} · ` +
                `diferencia ${item.difference_cop === null ? 'no calculada' : currency.format(item.difference_cop)} · ` +
                `${item.settlement_date || 'sin fecha de liquidación'}`,
        );
        renderRows(
            document.getElementById('admin-manual-movements'),
            data.manual_movements,
            (item) => `${item.movement_type} · ${item.seller_name || 'XrteShop'}`,
            (item) => `${currency.format(item.amount_cop)} · ${item.account_alias || 'sin alias'} · ` +
                `${item.movement_date} · ${item.reference} · ${item.note}`,
        );
    }

    async function loadBankAccounts() {
        const response = await fetch('/api/admin/accounts', { credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudieron cargar las cuentas.');
        const container = document.getElementById('admin-bank-accounts');
        container.replaceChildren();
        const accountSelect = document.getElementById('admin-movement-account');
        accountSelect.replaceChildren(element('option', 'Selecciona una cuenta', null));
        accountSelect.options[0].value = '';
        accountSelect.options[0].disabled = true;
        data.accounts.filter((account) => account.active).forEach((account) => {
            const option = element('option', `${account.alias} · ****${account.last_four}`);
            option.value = String(account.id);
            accountSelect.append(option);
        });
        if (!data.accounts.length) {
            container.append(element('p', 'No has registrado cuentas.', 'admin-empty'));
            return;
        }
        data.accounts.forEach((account) => {
            const row = element('article', null, 'admin-row admin-operation-row');
            row.append(element('strong', `${account.alias} · ${account.bank_name}`));
            row.append(element(
                'small',
                `${account.account_type} · ${account.account_holder} · ****${account.last_four} · ` +
                `${account.active ? 'activa' : 'inactiva'}${account.is_primary ? ' · principal' : ''}`,
            ));
            const edit = element('button', 'Editar datos');
            edit.type = 'button';
            edit.addEventListener('click', async () => {
                const alias = window.prompt('Nuevo alias de la cuenta:', account.alias);
                if (alias === null) return;
                const bankName = window.prompt('Banco o billetera:', account.bank_name);
                if (bankName === null) return;
                const holder = window.prompt('Titular:', account.account_holder);
                if (holder === null) return;
                const type = window.prompt('Tipo: ahorros, corriente o billetera', account.account_type);
                if (type === null) return;
                const number = window.prompt('Número nuevo (déjalo vacío para conservarlo):');
                if (number === null || !window.confirm('¿Guardar los cambios de esta cuenta?')) return;
                try {
                    const updates = {
                        alias: alias.trim(),
                        bank_name: bankName.trim(),
                        account_holder: holder.trim(),
                        account_type: type.trim(),
                    };
                    if (number.trim()) updates.account_number = number.trim();
                    await sendJson(`/api/admin/accounts/${account.id}`, 'PATCH', updates);
                    await loadBankAccounts();
                } catch (error) {
                    status.textContent = error.message;
                }
            });
            const primary = element('button', account.is_primary ? 'Cuenta principal' : 'Marcar principal');
            primary.type = 'button';
            primary.disabled = Boolean(account.is_primary || !account.active);
            primary.addEventListener('click', async () => {
                try {
                    await sendJson(`/api/admin/accounts/${account.id}`, 'PATCH', { is_primary: true });
                    await loadBankAccounts();
                } catch (error) {
                    status.textContent = error.message;
                }
            });
            const toggle = element('button', account.active ? 'Desactivar' : 'Reactivar');
            toggle.type = 'button';
            toggle.addEventListener('click', async () => {
                if (!window.confirm(`${account.active ? '¿Desactivar' : '¿Reactivar'} ${account.alias}?`)) return;
                try {
                    await sendJson(`/api/admin/accounts/${account.id}`, 'PATCH', { active: !account.active });
                    await loadBankAccounts();
                } catch (error) {
                    status.textContent = error.message;
                }
            });
            const reveal = element('button', 'Ver número completo');
            reveal.type = 'button';
            reveal.addEventListener('click', async () => {
                const currentPassword = window.prompt('Confirma tu contraseña actual:');
                if (currentPassword === null) return;
                reveal.disabled = true;
                try {
                    const result = await sendJson(
                        `/api/admin/accounts/${account.id}/reveal`,
                        'POST',
                        { current_password: currentPassword },
                    );
                    const number = element('small', `Número completo: ${result.account_number}`);
                    row.append(number);
                    window.setTimeout(() => number.remove(), 30000);
                } catch (error) {
                    status.textContent = error.message;
                } finally {
                    reveal.disabled = false;
                }
            });
            row.append(edit, primary, toggle, reveal);
            container.append(row);
        });
    }

    async function loadAdminAudit() {
        const response = await fetch('/api/admin/audit', { credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo cargar la auditoría.');
        renderRows(
            document.getElementById('admin-audit-actions'),
            data.actions,
            (item) => `${item.created_at} · ${item.action}`,
            (item) => `${item.actor_email || 'Administrador'} · ${item.affected_resource} · IP ${item.ip_address}`,
        );
        renderRows(
            document.getElementById('admin-audit-access'),
            data.access_events,
            (item) => `${item.created_at} · ${item.event}`,
            (item) => `IP ${item.ip_address} · ${item.device}`,
        );
    }

    async function loadAdminPeople() {
        const [sellerResponse, customerResponse] = await Promise.all([
            fetch('/api/admin/sellers', { credentials: 'same-origin' }),
            fetch('/api/admin/customers', { credentials: 'same-origin' }),
        ]);
        const [sellers, customers] = await Promise.all([
            sellerResponse.json(),
            customerResponse.json(),
        ]);
        if (!sellerResponse.ok) throw new Error(sellers.error || 'No se pudieron cargar emprendedores.');
        if (!customerResponse.ok) throw new Error(customers.error || 'No se pudieron cargar clientes.');
        const sellerContainer = document.getElementById('admin-sellers');
        sellerContainer.replaceChildren();
        sellers.sellers.forEach((seller) => {
            const row = element('article', null, 'admin-row admin-operation-row');
            row.append(element('strong', `${seller.name} · ${seller.email}`));
            row.append(element(
                'small',
                `${seller.product_count} productos · ventas ${currency.format(seller.sales_cop)} · ` +
                `saldo pendiente ${currency.format(seller.pending_cop)} · cuenta ${seller.verification_status || 'sin verificación'} · ` +
                `estado ${seller.account_status}`,
            ));
            const button = element('button', seller.account_status === 'suspended' ? 'Reactivar' : 'Suspender');
            button.type = 'button';
            button.addEventListener('click', async () => {
                const next = seller.account_status === 'suspended' ? 'active' : 'suspended';
                if (!window.confirm(`¿${next === 'active' ? 'Reactivar' : 'Suspender'} ${seller.name}?`)) return;
                button.disabled = true;
                try {
                    await sendJson(`/api/admin/sellers/${seller.id}/status`, 'PATCH', { status: next });
                    await loadAdminPeople();
                } catch (error) {
                    status.textContent = error.message;
                    button.disabled = false;
                }
            });
            row.append(button);
            sellerContainer.append(row);
        });
        if (!sellers.sellers.length) sellerContainer.append(element('p', 'No hay emprendedores.', 'admin-empty'));
        renderRows(
            document.getElementById('admin-customers'),
            customers.customers,
            (item) => `${item.name} · ${item.email}`,
            (item) => `${item.order_count} pedidos · ${item.phone || 'sin teléfono'} · ${item.created_at}`,
        );
    }

    async function loadAdminProducts() {
        const response = await fetch('/api/admin/products', { credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudieron cargar los productos.');
        const container = document.getElementById('admin-all-products');
        container.replaceChildren();
        data.products.forEach((product) => {
            const row = element('article', null, 'admin-row admin-operation-row');
            row.append(element('strong', `${product.name} · ${product.origin}`));
            row.append(element(
                'small',
                `${product.seller_name || 'XrteShop'} · ${product.category} · Stock ${product.stock} · ` +
                `Costo ${product.supplier_cost === null ? 'no aplica' : currency.format(product.supplier_cost)} · ` +
                `Flete ${currency.format(product.supplier_shipping_estimate)} · ` +
                `${product.active ? 'publicado' : 'pausado/borrador'}`,
            ));
            if (product.origin === 'proveedor' && product.supplier_url) {
                try {
                    const supplierUrl = new URL(product.supplier_url);
                    if (
                        supplierUrl.protocol === 'https:' &&
                        [
                            'mercadolibre.com.co',
                            'www.mercadolibre.com.co',
                            'articulo.mercadolibre.com.co',
                        ].includes(supplierUrl.hostname.toLowerCase())
                    ) {
                        const supplierLink = element('a', 'Comprar manualmente en Mercado Libre');
                        supplierLink.href = supplierUrl.href;
                        supplierLink.target = '_blank';
                        supplierLink.rel = 'noopener noreferrer';
                        row.append(supplierLink);
                    }
                } catch (_error) {
                    status.textContent = `El enlace de proveedor de ${product.name} no es válido.`;
                }
            }
            const price = document.createElement('input');
            price.type = 'number';
            price.min = '1';
            price.step = '1';
            price.value = String(product.price);
            price.setAttribute('aria-label', `Precio COP de ${product.name}`);
            const savePrice = element('button', 'Guardar precio');
            savePrice.type = 'button';
            savePrice.addEventListener('click', async () => {
                if (!window.confirm(`¿Cambiar el precio de ${product.name} a ${currency.format(Number(price.value))}?`)) return;
                savePrice.disabled = true;
                try {
                    await sendJson(`/api/admin/products/${encodeURIComponent(product.id)}`, 'PATCH', {
                        price: Number(price.value),
                    });
                    await loadAdminProducts();
                } catch (error) {
                    status.textContent = error.message;
                    savePrice.disabled = false;
                }
            });
            const toggle = element('button', product.active ? 'Pausar' : 'Aprobar/publicar');
            toggle.type = 'button';
            toggle.addEventListener('click', async () => {
                const next = !product.active;
                if (!window.confirm(`¿${next ? 'Publicar' : 'Pausar'} ${product.name}?`)) return;
                toggle.disabled = true;
                try {
                    await sendJson(`/api/admin/products/${encodeURIComponent(product.id)}`, 'PATCH', {
                        active: next,
                    });
                    await loadAdminProducts();
                } catch (error) {
                    status.textContent = error.message;
                    toggle.disabled = false;
                }
            });
            const deleteButton = element('button', 'Borrar producto', 'admin-delete-button');
            deleteButton.type = 'button';
            deleteButton.setAttribute('aria-label', `Borrar producto ${product.name}`);
            deleteButton.addEventListener('click', async () => {
                if (!window.confirm(
                    `¿Borrar ${product.name}? Si tiene pedidos o referencias históricas, ` +
                    'se conservará inactivo para proteger el historial.',
                )) return;
                deleteButton.disabled = true;
                try {
                    const result = await sendJson(
                        `/api/admin/products/${encodeURIComponent(product.id)}`,
                        'DELETE',
                    );
                    await loadAdminProducts();
                    status.textContent = result.message;
                } catch (error) {
                    status.textContent = error.message;
                    deleteButton.disabled = false;
                }
            });
            row.append(price, savePrice, toggle, deleteButton);
            container.append(row);
        });
        if (!data.products.length) container.append(element('p', 'No hay productos.', 'admin-empty'));
    }

    async function loadAdminOrders(filters = {}) {
        const query = new URLSearchParams(filters);
        Array.from(query.keys()).forEach((key) => {
            if (!query.get(key)) query.delete(key);
        });
        const response = await fetch(
            `/api/admin/orders${query.toString() ? `?${query}` : ''}`,
            { credentials: 'same-origin' },
        );
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudieron cargar los pedidos.');
        const container = document.getElementById('admin-all-orders');
        container.replaceChildren();
        if (!data.orders.length) {
            container.append(element('p', 'No hay pedidos con estos filtros.', 'admin-empty'));
            return;
        }
        data.orders.forEach((order) => {
            const needsAttention = ['pending', 'partial', 'failed', 'expired'].includes(order.payment_status);
            const card = element('article', null, `admin-row${needsAttention ? ' admin-order-attention' : ''}`);
            card.append(element(
                'strong',
                `Pedido #${order.id} · ${order.customer_name} · ${currency.format(order.total)} · ${order.payment_status}`,
            ));
            card.append(element(
                'small',
                `${order.email} · ${order.phone || 'sin teléfono'} · ${order.address}, ${order.city}, ${order.department} ` +
                `${order.postal_code || ''} · ${order.delivery_notes || ''} · Pago ${order.payment_method || 'sin método'} · ` +
                `${order.status} · ${order.created_at}`,
            ));
            card.append(element(
                'small',
                order.items.map((item) =>
                    `${item.quantity} × ${item.product_name} · ${item.origin || 'sin origen'} · ` +
                    `precio ${currency.format(item.unit_price)} · costo ${item.supplier_cost === null
                        ? 'no aplica'
                        : currency.format(item.supplier_cost)} · flete ${currency.format(item.supplier_shipping_estimate || 0)}`
                ).join('\n'),
            ));
            order.shipments.forEach((shipment) => {
                const shipmentRow = element('form', null, 'admin-operation-row');
                const splitLabel = element(
                    'small',
                    `Envío ${shipment.split_id} · ${shipment.shipping_status} · ` +
                    `${shipment.shipping_carrier || 'sin transportadora'} · ${shipment.tracking_number || 'sin guía'}`,
                );
                const state = document.createElement('select');
                state.name = 'shipping_status';
                [
                    ['preparing', 'Preparando'],
                    ['shipped', 'Enviado'],
                    ['in_transit', 'En tránsito'],
                    ['delivered', 'Entregado'],
                ].forEach(([value, label]) => {
                    const option = element('option', label);
                    option.value = value;
                    option.selected = value === shipment.shipping_status;
                    state.append(option);
                });
                const carrier = document.createElement('input');
                carrier.name = 'shipping_carrier';
                carrier.maxLength = 80;
                carrier.placeholder = 'Transportadora';
                carrier.value = shipment.shipping_carrier || '';
                const tracking = document.createElement('input');
                tracking.name = 'tracking_number';
                tracking.maxLength = 100;
                tracking.placeholder = 'Número de guía';
                tracking.value = shipment.tracking_number || '';
                const update = element('button', 'Actualizar envío');
                update.type = 'submit';
                shipmentRow.append(splitLabel, state, carrier, tracking, update);
                shipmentRow.addEventListener('submit', async (event) => {
                    event.preventDefault();
                    if (!window.confirm(`¿Actualizar el estado del envío del pedido #${order.id}?`)) return;
                    update.disabled = true;
                    try {
                        await sendJson(`/api/admin/orders/${order.id}/shipping`, 'PATCH', {
                            split_id: shipment.split_id,
                            shipping_status: state.value,
                            shipping_carrier: carrier.value.trim(),
                            tracking_number: tracking.value.trim(),
                        });
                        await loadAdminOrders(filters);
                    } catch (error) {
                        status.textContent = error.message;
                    } finally {
                        update.disabled = false;
                    }
                });
                card.append(shipmentRow);
            });
            const refund = element('button', 'Solicitar reembolso');
            refund.type = 'button';
            refund.disabled = !['approved', 'partially_refunded'].includes(order.payment_status);
            refund.addEventListener('click', () => {
                const form = document.getElementById('admin-refund-form');
                form.elements.order_id.value = order.id;
                form.scrollIntoView({ behavior: 'smooth', block: 'center' });
            });
            card.append(refund);
            container.append(card);
        });
    }

    async function loadAdminFinance() {
        const response = await fetch('/api/admin/finance', { credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo cargar finanzas.');
        renderFinance(data);
    }

    async function loadAdminSettings() {
        const response = await fetch('/api/admin/settings', { credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo cargar la configuración.');
        const form = document.getElementById('admin-settings-form');
        form.elements.commission_rate.value = data.commission_rate;
        form.elements.markup_rate.value = data.markup_rate;
        form.elements.iva_rate.value = data.iva_rate;
        document.getElementById('admin-payment-settings-readonly').textContent =
            `Comisión fija de XrteShop: ${Number(data.commission_rate) * 100}% · ` +
            `Métodos permitidos: ${data.allowed_payment_methods.join(', ')} · ` +
            `Wompi: ${data.payment_environment === 'sandbox' ? 'MODO PRUEBAS' : data.payment_environment}. ` +
            'El entorno de pagos se configura en Vercel y no se cambia desde aquí.';
    }

    async function loadMonthlyReport(month) {
        const response = await fetch(
            `/api/admin/finance/monthly?month=${encodeURIComponent(month)}`,
            { credentials: 'same-origin' },
        );
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'No se pudo generar el reporte.');
        const report = result.report;
        document.getElementById('admin-monthly-report').textContent =
            `${report.month} · Ventas ${currency.format(report.gross_sales_cop)} · ` +
            `Reembolsos ${currency.format(report.refunds_cop)} · ` +
            `Comisión ${currency.format(report.commission_cop)} · ` +
            `IVA estimado (${Number(report.iva_rate) * 100}%) ${currency.format(report.iva_estimate_cop)} · ` +
            `${report.iva_note}`;
        document.getElementById('admin-monthly-export').href =
            `/api/admin/finance/monthly.csv?month=${encodeURIComponent(month)}`;
    }

    const cleanupPreviewButton = document.getElementById('admin-catalog-preview');
    const cleanupResults = document.getElementById('admin-catalog-preview-results');
    const cleanupForm = document.getElementById('admin-catalog-cleanup-form');
    const cleanupStatus = document.getElementById('admin-catalog-cleanup-status');
    const cleanupConfirmation = cleanupForm.elements.confirmation;
    const cleanupApplyButton = cleanupForm.querySelector('button[type="submit"]');

    cleanupPreviewButton.addEventListener('click', async () => {
        cleanupPreviewButton.disabled = true;
        cleanupStatus.textContent = 'Creando copia de seguridad y revisando el catálogo…';
        cleanupResults.replaceChildren();
        catalogCleanupSnapshot = null;
        cleanupConfirmation.value = '';
        cleanupConfirmation.disabled = true;
        cleanupApplyButton.disabled = true;
        try {
            const preview = await sendJson('/api/admin/catalog-cleanup/preview', 'POST', {});
            catalogCleanupSnapshot = preview;
            cleanupResults.append(
                element(
                    'p',
                    `Backup: ${preview.backup_directory} · ` +
                    `${preview.will_delete} sin historial para eliminar · ` +
                    `${preview.will_archive} con historial para archivar · ` +
                    `${preview.entrepreneur_products_untouched} productos de emprendedores protegidos · ` +
                    `${preview.provider_products_preserved} productos de proveedor protegidos.`,
                ),
            );
            preview.targets.forEach((item) => {
                cleanupResults.append(
                    element(
                        'p',
                        `${item.id} · ${item.name} · ${item.action === 'archive' ? 'Se archivará' : 'Se eliminará'} · ` +
                        `${item.order_count} líneas de pedido · ${item.supplier_references} referencias de proveedor · ` +
                        `${item.other_references} otras referencias`,
                        'admin-row',
                    ),
                );
            });
            cleanupConfirmation.disabled = preview.targets.length === 0;
            cleanupApplyButton.disabled = preview.targets.length === 0;
            cleanupConfirmation.placeholder = preview.confirmation_phrase;
            cleanupStatus.textContent = preview.targets.length
                ? `Revisa la lista y el backup. Para aplicar, escribe: ${preview.confirmation_phrase}`
                : 'No hay productos heredados para limpiar. Las publicaciones de emprendedores y proveedor están protegidas.';
        } catch (error) {
            cleanupStatus.textContent = error.message;
        } finally {
            cleanupPreviewButton.disabled = false;
        }
    });

    cleanupConfirmation.addEventListener('input', () => {
        cleanupApplyButton.disabled = !catalogCleanupSnapshot ||
            cleanupConfirmation.value !== catalogCleanupSnapshot.confirmation_phrase;
    });

    cleanupForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (!catalogCleanupSnapshot || cleanupConfirmation.value !== catalogCleanupSnapshot.confirmation_phrase) {
            cleanupStatus.textContent = 'Vuelve a previsualizar y escribe la frase exacta de confirmación.';
            return;
        }
        const deletionCount = catalogCleanupSnapshot.will_delete;
        const archiveCount = catalogCleanupSnapshot.will_archive;
        if (!window.confirm(
            `Se eliminarán ${deletionCount} productos sin referencias y se archivarán ${archiveCount} que tienen historial. ` +
            'Los productos heredados propios saldrán del catálogo; se conservan las publicaciones de emprendedores y proveedor. ¿Confirmas aplicar la limpieza?',
        )) return;
        cleanupApplyButton.disabled = true;
        cleanupPreviewButton.disabled = true;
        cleanupStatus.textContent = 'Aplicando la limpieza…';
        try {
            const result = await sendJson('/api/admin/catalog-cleanup/apply', 'POST', {
                confirmation: cleanupConfirmation.value,
                expected_ids: catalogCleanupSnapshot.targets.map((item) => item.id),
            });
            cleanupStatus.textContent =
                `Completado. Eliminados: ${result.deleted_ids.length}; archivados: ${result.archived_ids.length}. ` +
                `Backup: ${result.backup_directory}`;
            catalogCleanupSnapshot = null;
            cleanupForm.reset();
            cleanupConfirmation.disabled = true;
            window.location.reload();
        } catch (error) {
            cleanupStatus.textContent = error.message;
            cleanupPreviewButton.disabled = false;
            cleanupApplyButton.disabled = false;
        }
    });

    try {
        const sessionResponse = await fetch('/api/admin/session', { credentials: 'same-origin' });
        const sessionData = await sessionResponse.json();
        if (!sessionResponse.ok || !sessionData.ok) {
            window.location.assign('/cuenta?next=%2Fadmin');
            return;
        }
        csrfToken = sessionData.csrf_token;
        const response = await fetch('/api/admin/dashboard', { credentials: 'same-origin' });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'No se pudo cargar el panel.');
        const metrics = document.getElementById('admin-metrics');
        addMetric(metrics, 'Ventas pagadas · 30 días', currency.format(data.analytics.gross_sales_30d));
        addMetric(metrics, 'Pedidos pagados · 30 días', String(data.analytics.paid_orders_30d));
        addMetric(metrics, 'Cupones canjeados · 30 días', `${data.analytics.coupons_redeemed_30d} · ${data.analytics.coupon_redemption_rate_30d}%`);
        addMetric(metrics, 'Puntos de fidelidad vigentes', String(data.analytics.loyalty_points_outstanding));
        addMetric(metrics, 'Mensajes recibidos', String(data.messages.length));
        addMetric(metrics, 'Suscriptores activos', String(data.newsletter_subscribers));
        addMetric(metrics, 'Comisiones acumuladas', currency.format(data.analytics.commissions_accumulated));
        addMetric(metrics, 'Payouts pendientes', currency.format(data.analytics.payouts_pending));
        addMetric(metrics, 'Reembolsos', currency.format(data.analytics.refunds_total));
        addMetric(metrics, 'Disputas abiertas', String(data.analytics.open_disputes));
        addMetric(metrics, 'Recuperaciones a vendedores', currency.format(data.analytics.seller_recoveries_open));

        const interactionLabels = {
            view: 'Vistas guardadas',
            cart_add: 'Añadidos a la bolsa',
            search_result: 'Resultados de búsqueda',
        };
        const interactions = Object.entries(data.analytics.product_interactions_30d);
        const maximum = Math.max(1, ...interactions.map(([, count]) => count));
        const bars = document.getElementById('admin-interactions');
        bars.replaceChildren();
        interactions.forEach(([action, count]) => {
            const row = document.createElement('div');
            row.className = 'admin-bar';
            const label = document.createElement('span');
            label.textContent = interactionLabels[action] || action;
            const track = document.createElement('span');
            track.className = 'admin-bar-track';
            const fill = document.createElement('span');
            fill.style.width = `${Math.round(count / maximum * 100)}%`;
            track.append(fill);
            const amount = document.createElement('strong');
            amount.textContent = String(count);
            row.append(label, track, amount);
            bars.append(row);
        });
        renderRows(
            document.getElementById('admin-top-products'),
            data.analytics.top_interacted_products,
            (item) => item.name,
            (item) => `${item.count} interacciones`,
        );
        renderRows(
            document.getElementById('admin-messages'),
            data.messages,
            (item) => item.subject,
            (item) => `${item.name} · ${item.email} · ${item.created_at}\n${item.message}`,
        );
        renderRows(
            document.getElementById('admin-orders'),
            data.orders,
            (item) => `Pedido #${item.id} · ${item.customer_name}`,
            (item) => `${currency.format(item.total)} · ${item.payment_status} · ${item.status} · ${item.created_at}`,
        );
        renderRows(
            document.getElementById('admin-products'),
            data.products,
            (item) => item.name,
            (item) => `${item.seller_name} · ${item.category} · ${item.stock} unidades · ${item.active ? 'Activa' : 'Pausada'}`,
        );
        renderPayouts(data.payment_payouts);
        renderRows(
            document.getElementById('admin-payment-refunds'),
            data.payment_refunds,
            (item) => `Reembolso #${item.id} · pedido #${item.order_id}`,
            (item) => `${currency.format(item.amount)} · comisión revertida ${currency.format(item.commission_reversed)} · ${item.status} · ${item.provider_refund_id || 'sin ID Wompi'}`,
        );
        renderRefundLiabilities(data.payment_refund_liabilities);
        renderDisputes(data.payment_disputes);
        let supplierPanelError = '';
        try {
            const supplierResponse = await fetch('/api/admin/suppliers', { credentials: 'same-origin' });
            const supplierData = await supplierResponse.json();
            if (!supplierResponse.ok) {
                throw new Error(supplierData.error || 'No se pudo cargar el panel de proveedores.');
            }
            renderRows(
                document.getElementById('admin-suppliers'),
                supplierData.suppliers,
                (item) => `${item.name} · ${item.connector}`,
                (item) => `${item.api_status} · ${item.enabled ? 'habilitado' : 'desactivado'} · ` +
                    `${item.payment_mode} · ${item.linked_products} productos · ${item.open_orders} órdenes abiertas · ` +
                    `costo pendiente ${currency.format(item.open_order_cost)} · ` +
                    `última sincronización ${item.last_synced_at || 'nunca'}`,
            );
            renderRows(
                document.getElementById('admin-supplier-orders'),
                supplierData.open_orders,
                (item) => `Pedido #${item.order_id} · ${item.supplier_name} · ${item.status}`,
                (item) => `Costo ${currency.format(item.supplier_cost)} · ${item.attempts} intentos · ` +
                    `guía ${item.tracking_number || 'pendiente'} · ${item.updated_at}`,
            );
            renderRows(
                document.getElementById('admin-supplier-sync-logs'),
                supplierData.sync_logs,
                (item) => `${item.supplier_name} · ${item.status} · ${item.started_at}`,
                (item) => `${item.products_seen} productos consultados · ${item.products_updated} actualizados · ` +
                    `${item.completed_at || 'sin finalizar'}`,
            );
        } catch (error) {
            supplierPanelError = error.message;
        }
        let financeError = '';
        try {
            await loadAdminFinance();
            await loadBankAccounts();
            await loadAdminAudit();
            await loadAdminSettings();
            const month = new Date().toISOString().slice(0, 7);
            document.querySelector('#admin-monthly-report-form [name="month"]').value = month;
            await loadMonthlyReport(month);
            await loadAdminPeople();
            await loadAdminProducts();
            await loadAdminOrders();
        } catch (error) {
            financeError = error.message;
        }
        status.textContent = [
            'Panel actualizado.',
            supplierPanelError ? `Proveedores: ${supplierPanelError}` : '',
            financeError ? `Finanzas y seguridad: ${financeError}` : '',
        ].filter(Boolean).join(' ');
    } catch (error) {
        status.textContent = error.message;
    }

    document.getElementById('admin-refund-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = document.getElementById('admin-refund-status');
        const button = form.querySelector('button[type="submit"]');
        if (!window.confirm('Confirma que deseas solicitar el reembolso al método original.')) return;
        button.disabled = true;
        message.textContent = 'Enviando solicitud de reembolso a Wompi…';
        try {
            const orderId = Number(form.elements.order_id.value);
            const result = await sendJson(`/api/admin/orders/${orderId}/refunds`, 'POST', {
                amount: Number(form.elements.amount.value),
                reason: form.elements.reason.value.trim(),
            });
            message.textContent = `Estado del reembolso: ${result.status}. ${result.message || ''}`;
            if (result.status === 'completed') window.location.reload();
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    document.getElementById('admin-dispute-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = document.getElementById('admin-dispute-status');
        const button = form.querySelector('button[type="submit"]');
        button.disabled = true;
        message.textContent = 'Registrando disputa verificada…';
        try {
            const amount = form.elements.amount.value;
            await sendJson(`/api/admin/orders/${Number(form.elements.order_id.value)}/disputes`, 'POST', {
                provider_dispute_id: form.elements.provider_dispute_id.value.trim(),
                payment_id: form.elements.payment_id.value.trim(),
                amount: amount ? Number(amount) : 0,
            });
            window.location.reload();
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    document.getElementById('admin-account-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = document.getElementById('admin-account-status');
        const button = form.querySelector('button[type="submit"]');
        if (!window.confirm('¿Guardar esta cuenta cifrada para conciliación?')) return;
        button.disabled = true;
        try {
            await sendJson('/api/admin/accounts', 'POST', {
                bank_name: form.elements.bank_name.value.trim(),
                account_type: form.elements.account_type.value,
                account_holder: form.elements.account_holder.value.trim(),
                alias: form.elements.alias.value.trim(),
                account_number: form.elements.account_number.value,
                is_primary: form.elements.is_primary.checked,
            });
            form.reset();
            message.textContent = 'Cuenta guardada; el número no se devuelve al navegador.';
            await loadBankAccounts();
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    document.getElementById('admin-password-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = document.getElementById('admin-password-status');
        const button = form.querySelector('button[type="submit"]');
        if (!window.confirm('Cambiar la contraseña cerrará todas las sesiones. ¿Continuar?')) return;
        button.disabled = true;
        try {
            const result = await sendJson('/api/admin/security/change-password', 'POST', {
                current_password: form.elements.current_password.value,
                new_password: form.elements.new_password.value,
            });
            message.textContent = result.message;
            window.location.replace('/cuenta');
        } catch (error) {
            message.textContent = error.message;
            button.disabled = false;
        }
    });

    document.getElementById('admin-reconciliation-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = document.getElementById('admin-reconciliation-status');
        const button = form.querySelector('button[type="submit"]');
        button.disabled = true;
        try {
            const response = await fetch('/api/admin/finance/reconciliation/import', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'X-CSRF-Token': csrfToken },
                body: new FormData(form),
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || 'No se pudo importar el CSV.');
            message.textContent = `${result.message} Filas: ${result.imported}.`;
            form.reset();
            await loadAdminFinance();
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    document.getElementById('admin-manual-movement-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = document.getElementById('admin-manual-movement-status');
        const button = form.querySelector('button[type="submit"]');
        const movementType = form.elements.movement_type.value;
        if (!window.confirm('Confirma que el movimiento ya ocurrió; esto solo lo registra y no transfiere dinero.')) return;
        button.disabled = true;
        try {
            const sellerValue = form.elements.seller_id.value;
            const result = await sendJson('/api/admin/finance/manual-movements', 'POST', {
                movement_type: movementType,
                amount_cop: Number(form.elements.amount_cop.value),
                movement_date: form.elements.movement_date.value,
                account_id: Number(form.elements.account_id.value),
                seller_id: sellerValue ? Number(sellerValue) : null,
                reference: form.elements.reference.value.trim(),
                note: form.elements.note.value.trim(),
            });
            message.textContent = result.message;
            form.reset();
            await loadAdminFinance();
            await loadBankAccounts();
            await loadAdminAudit();
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    document.getElementById('admin-settings-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const message = document.getElementById('admin-settings-status');
        const button = form.querySelector('button[type="submit"]');
        button.disabled = true;
        try {
            const result = await sendJson('/api/admin/settings', 'PATCH', {
                commission_rate: form.elements.commission_rate.value,
                markup_rate: form.elements.markup_rate.value,
                iva_rate: form.elements.iva_rate.value,
            });
            message.textContent = result.message;
            await loadAdminSettings();
        } catch (error) {
            message.textContent = error.message;
        } finally {
            button.disabled = false;
        }
    });

    document.getElementById('admin-finance-filter-form').addEventListener('change', (event) => {
        const form = event.currentTarget;
        const query = new URLSearchParams(new FormData(form));
        Array.from(query.keys()).forEach((key) => {
            if (!query.get(key)) query.delete(key);
        });
        const queryString = query.toString();
        document.getElementById('admin-finance-export').href =
            `/api/admin/finance/export.csv${queryString ? `?${queryString}` : ''}`;
    });

    document.getElementById('admin-monthly-report-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        try {
            await loadMonthlyReport(form.elements.month.value);
        } catch (error) {
            document.getElementById('admin-monthly-report').textContent = error.message;
        }
    });

    document.getElementById('admin-order-filter-form').addEventListener('submit', async (event) => {
        event.preventDefault();
        const values = Object.fromEntries(new FormData(event.currentTarget));
        try {
            await loadAdminOrders(values);
        } catch (error) {
            status.textContent = error.message;
        }
    });
});
