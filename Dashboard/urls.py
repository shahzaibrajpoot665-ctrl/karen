from django.urls import path,include
from . import views
urlpatterns = [
    path('',views.form_view, name='form'),
    path('product_detail/<int:id>/',views.product_detail, name='product_detail'),

    path('single_product/',views.single_product, name='single_product'),
    path('product_api/',views.product_api, name='product_api'),

    path('login/',views.login_view, name='login'),
    path('user_logout/', views.user_logout, name='user_logout'),
    path('change_password/', views.change_password, name='change_password'),
    path('export_excel/', views.export_to_excel, name='export_excel'),
    path('export_selected_to_excel/', views.export_selected_to_excel, name='export_selected_to_excel'),
    path('pairing_set/',views.pairing_set_view, name='pairing_set'),
    path('pairing_set_print/',views.pairing_set_print_view, name='pairing_set_print'),
    path('pairing_set_api/',views.pairing_set_api, name='pairing_set_api'),
    path('upload_bulk_images/',views.upload_bulk_images, name='upload_bulk_images'),
    path('product_import/start/', views.product_import_start, name='product_import_start'),
    path('product_import/status/', views.product_import_status, name='product_import_status'),
    path('image_management/', views.image_management, name='image_management'),
    path('image_api/', views.image_api, name='image_api'),
    path('get_unlinked_images/', views.get_unlinked_images, name='get_unlinked_images'),
    path('search_products_for_linking/', views.search_products_for_linking, name='search_products_for_linking'),
    path('api/product/<int:product_id>/images/', views.product_images_api, name='product_images_api'),
    
    # Cart Management URLs
    path('cart-management/', views.cart_management_view, name='cart_management'),
    path('api/customers/', views.customer_api, name='customer_api'),
    path('customer/<int:customer_id>/cart/', views.customer_cart_view, name='customer_cart'),
    path('api/customer/<int:customer_id>/cart/', views.cart_api, name='cart_api'),
    path('customer/<int:customer_id>/cart/export/', views.export_customer_cart_excel, name='export_customer_cart_excel'),
    path('customer/<int:customer_id>/cart/import/', views.import_customer_cart_excel, name='import_customer_cart_excel'),
    path('customer/<int:customer_id>/cart/print/', views.print_customer_cart, name='print_customer_cart'),
    
    # Android App API
    path('api/add-to-cart/', views.add_to_cart_api, name='add_to_cart_api'),
    path('api/customers-android/', views.customers_android_api, name='customers_android_api'),
    path('api/customers-android/delete/', views.customers_android_delete_api, name='customers_android_delete_api'),
    path('api/customers-android/create/', views.customers_android_create_api, name='customers_android_create_api'),
    path('api/customers-android/lock/', views.customers_android_lock_api, name='customers_android_lock_api'),
    path('api/customers-android/locked-count/', views.customers_android_locked_count_api, name='customers_android_locked_count_api'),
    path('api/customers-android/locked-ids/', views.customers_android_locked_ids_api, name='customers_android_locked_ids_api'),
    path('api/cart-android/add-bulk/', views.add_to_cart_bulk_android, name='add_to_cart_bulk_android'),
    path('api/customer/<int:customer_id>/cart-android/', views.cart_android_api, name='cart_android_api'),
]
