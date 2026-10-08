document.addEventListener('DOMContentLoaded', function() {
    // Recupera el carrito (si existe) o crea uno nuevo
    let cart = JSON.parse(localStorage.getItem('cart')) || [];
    
    const cartCount = document.getElementById('cart-count');
    const cartList = document.getElementById('cart-list');
    const simpleCart = document.getElementById('simple-cart');
    // El enlace del carrito es el 4to li de .nav-links
    const cartLink = document.querySelector('.nav-links li:nth-child(4) a');
    const clearCartButton = document.getElementById('clear-cart');
    const closeCartButton = document.getElementById('close-cart');
    
    updateCart();
    
    // Al hacer clic en el enlace del carrito, se muestra/oculta el contenedor
    cartLink.addEventListener('click', function(e) {
      e.preventDefault();
      simpleCart.classList.toggle('visible');
    });
    
    // Botón para cerrar el carrito
    closeCartButton.addEventListener('click', function() {
      simpleCart.classList.remove('visible');
    });
    
    // Botón para vaciar el carrito
    clearCartButton.addEventListener('click', function() {
      cart = [];
      updateCart();
    });
    
    // Añadir producto al carrito al hacer clic en "Agregar al carrito"
    document.querySelectorAll('.add-to-cart').forEach(button => {
      button.addEventListener('click', function() {
        // Localiza el contenedor del producto
        const productCard = this.closest('.product-card');
        // Obtiene el nombre
        const productName = productCard.querySelector('h3').textContent;
        // Obtiene el precio y elimina caracteres no numéricos ($ y puntos)
        const priceText = productCard.querySelector('.price').textContent;
        const productPrice = parseFloat(priceText.replace(/[\$,\.]/g, ''));
        
        // Verifica si el producto ya está en el carrito (por nombre)
        const existingItem = cart.find(item => item.name === productName);
        if (existingItem) {
          existingItem.quantity++;
        } else {
          cart.push({
            name: productName,
            price: productPrice,
            quantity: 1
          });
        }
        
        updateCart();
        
        // Cambio visual del botón para indicar que el producto se agregó
        const originalText = this.innerHTML;
        this.innerHTML = '<i class="fas fa-check"></i> Agregado';
        this.classList.add('added');
        setTimeout(() => {
          this.innerHTML = originalText;
          this.classList.remove('added');
        }, 1500);
      });
    });
    
    // Función para actualizar el carrito, la lista y el contador
    function updateCart() {
      cartList.innerHTML = '';
      let total = 0;
      
      cart.forEach(item => {
        const li = document.createElement('li');
        li.innerHTML = `
          <span>${item.name} (${item.quantity})</span>
          <span>COP ${item.price * item.quantity}</span>
        `;
        total += item.price * item.quantity;
        cartList.appendChild(li);
      });
      
      cartCount.textContent = cart.reduce((sum, item) => sum + item.quantity, 0);
      localStorage.setItem('cart', JSON.stringify(cart));
    
      // Si el carrito tiene elementos, se muestra. Si está vacío, se oculta.
      if (cart.length > 0) {
        simpleCart.classList.add('visible');
      } else {
        simpleCart.classList.remove('visible');
      }
    }
  });
  