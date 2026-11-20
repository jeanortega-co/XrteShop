# Colombia: compatibilidad de métodos de pago y proveedores

Verificación de fuentes oficiales consultadas el 2 de noviembre de 2026. **No se debe interpretar “sin verificar” como soporte.** La disponibilidad de un método de pago no demuestra que acepte split directo.

## Métodos de pago

| Método pedido | Mercado Pago Colombia | Wompi Colombia | PayU Colombia | ePayco Colombia |
|---|---|---|---|---|
| Nequi | Sin verificar en su checkout público | Sí; split no verificado | Sí; split no verificado | Sin verificar |
| DaviPlata | Sin verificar | Sí; split no verificado | Sin verificar en la lista pública revisada | Sí; split no verificado por método |
| Nu, cuenta de ahorros | Sin verificar como riel | Sin verificar como riel | Sin verificar como riel | Sin verificar como riel |
| Nu, tarjeta débito/crédito | Tarjetas sí; emisor Nu sin verificar | Tarjetas sí; emisor Nu sin verificar | Tarjetas sí; emisor Nu sin verificar | Tarjetas sí; emisor Nu sin verificar |
| Mastercard | Tarjetas sí; marca sin verificar en la evidencia revisada | Sí; split no verificado | Tarjetas sí; marca sin verificar en la guía revisada | Tarjetas sí; aceptación live/split no verificada por marca |
| Visa | Tarjetas sí; marca sin verificar en la evidencia revisada | Sí; split no verificado | Tarjetas sí; marca sin verificar en la guía revisada | Tarjetas sí; aceptación live/split no verificada por marca |

| Proveedor | Split directo a la hora del cobro | Cuenta receptora | Requisitos y conclusión |
|---|---|---|---|
| Mercado Pago | No verificado para marketplace colombiano en las fuentes revisadas | No verificada para split | Su checkout documenta métodos, no el reparto directo solicitado. |
| Wompi | No demostrado. Su producto de pagos a terceros no es evidencia de split de una compra | No verificada en la documentación citada | Sus planes colombianos nombran Nequi, DaviPlata, Mastercard y Visa. La opción QR de Nequi tiene restricción de persona natural. |
| PayU | No demostrado; Payouts envía fondos posteriores desde el saldo del comercio | Cuenta bancaria colombiana del beneficiario | Payouts requiere habilitación bajo análisis de riesgo/seguridad y no equivale a split directo. |
| ePayco | La documentación del modelo agregador muestra split checkout en Colombia | Receptores/comercios registrados en ePayco; banco receptor directo no establecido | Requiere modelo agregador, activación por soporte y receptores registrados/asociados. Compatibilidad método-por-método no verificada. |

**Recomendación condicionada:** ePayco es el único de los cuatro con documentación consultada que demuestra split directo genérico para Colombia. Antes de escogerlo, solicitar confirmación escrita de que Nequi, DaviPlata, Visa y Mastercard funcionan con `splitpayment` y de que Nu está permitido (si Nu se refiere a tarjeta, debe procesarse como Visa/Mastercard; Nu cuenta no es un riel verificado). No habilitar ningún método hasta validar credenciales, contrato y pruebas del comercio.

### Restricción de checkout y firma

- ePayco Checkout v1 documenta `splitpayment`, `split_rule` y `split_receivers`; está condicionado al modelo agregador, activación por soporte y receptores registrados. No se debe mezclar su formato con el `splitPayment` de Smart Checkout v2.
- Smart Checkout v2 expone `methodsDisable`, pero la documentación consultada no define los valores válidos ni un mecanismo de allowlist. No prueba que la tienda pueda limitarse exactamente a los métodos solicitados.
- PayU WebCheckout permite que el comprador elija el método en la página alojada. En la guía consultada no aparece un parámetro documentado para restringir la lista.
- La confirmación ePayco documenta `x_signature` con SHA-256 de `p_cust_id_cliente^p_key^x_ref_payco^x_transaction_id^x_amount^x_currency_code`; la firma debe validarse en el servidor.

Por este motivo, el formulario muestra solo la allowlist Nequi/DaviPlata/Nu/Mastercard/Visa, pero el backend rechaza crear pedidos hasta que se confirme por escrito cómo restringir realmente el checkout y cuáles métodos están habilitados. `PEMU_PAYU_METHOD_RESTRICTION_VERIFIED` y `PEMU_PAYU_VERIFIED_METHODS` deben permanecer vacíos/apagados hasta confirmar esa configuración; no basta con seleccionar un método en el frontend.

Fuentes oficiales:

- Mercado Pago [Checkout API Colombia](https://www.mercadopago.com.co/developers/es/docs/checkout-api/landing).
- Wompi [Planes y tarifas Colombia](https://wompi.com/es/co/planes-tarifas/) y [solución de pagos a terceros](https://wompi.com/es/co/que-es-wompi).
- PayU [Payments API Colombia](https://developers.payulatam.com/latam/es/docs/integrations/api-integration/payments-api-colombia.html) y [Payouts Colombia](https://developers.payulatam.com/latam/es/docs/services/payouts.html).
- ePayco [API](https://docs.epayco.com/docs/api.md), [Split Agregador](https://docs.epayco.com/docs/split-descripcion.md) y [medios de prueba](https://docs.epayco.com/docs/medios-de-pruebas-1.md).
- ePayco [Smart Checkout](https://docs.epayco.com/docs/checkout-implementacion.md) y [confirmación y respuesta](https://docs.epayco.com/docs/checkout-respuesta-y-confirmacion.md).
- PayU [WebCheckout](https://developers.payulatam.com/latam/es/docs/integrations/webcheckout-integration.html) y [métodos de pago](https://developers.payulatam.com/latam/es/docs/getting-started/select-your-payment-method.html).
- Nu [Cuenta Nu](https://nu.com.co/cuenta) y [Nu Colombia](https://nu.com.co/).

## APIs de proveedores y dropshipping

| Proveedor | Estado de verificación | Evidencia/capacidades confirmadas |
|---|---|---|
| AliExpress Dropshipping API | Pendiente | El portal oficial confirma plataforma de desarrolladores, pero no se verificaron métodos de dropshipping, autorización para la cuenta ni elegibilidad de envío a Colombia. [Open Platform](https://openservice.aliexpress.com/) |
| MercadoLibre API | Limitado; no usar como proveedor de órdenes | API para cuenta autorizada de marketplace y sus ventas, no para fabricar compras de clientes en otra tienda. No se verificó cancelación en las fuentes accesibles. [Autorización](https://developers.mercadolibre.com.co/es_co/autenticacion-y-autorizacion), [publicaciones](https://developers.mercadolibre.com.co/es_co/publica-productos), [ventas](https://developers.mercadolibre.com.co/es_co/gestiona-ventas). |

El importador de productos de Mercado Libre usa su API pública oficial; no realiza scraping ni automatiza compras. Los conectores de órdenes y cumplimiento siguen desactivados hasta validar credenciales y operaciones contractualmente.
