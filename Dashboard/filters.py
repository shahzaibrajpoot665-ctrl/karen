import django_filters
from .models import *
from django.db.models import Q


class ProductFilter(django_filters.FilterSet):
    search = django_filters.CharFilter(method='filter_free_search', label='Search')
    childcode = django_filters.CharFilter(field_name='child_code', lookup_expr='icontains')
    search_tag = django_filters.CharFilter(field_name='tag__name', lookup_expr='iexact')
    
    # Advanced search filters
    parent_code = django_filters.CharFilter(field_name='parent_code', lookup_expr='icontains')
    child_code = django_filters.CharFilter(field_name='child_code', lookup_expr='icontains')
    location = django_filters.CharFilter(field_name='location', lookup_expr='icontains')
    kpo = django_filters.CharFilter(field_name='kpo', lookup_expr='icontains')
    price_min = django_filters.NumberFilter(field_name='thai_baht', lookup_expr='gte')
    price_max = django_filters.NumberFilter(field_name='thai_baht', lookup_expr='lte')
    
    class Meta:
        model = Product
        fields = ['search', 'childcode', 'search_tag', 'parent_code', 'child_code', 'location', 'kpo', 'price_min', 'price_max']

    def filter_free_search(self, queryset, name, value):
        # Handle combined format like "ELEC-002 - ELEC-002-E" (parent_code - child_code)
        if ' - ' in value:
            parts = value.split(' - ', 1)
            if len(parts) == 2:
                parent_part, child_part = parts
                return queryset.filter(
                    Q(parent_code__icontains=parent_part.strip()) &
                    Q(child_code__icontains=child_part.strip())
                ).distinct()
        
        # Default search across all fields
        return queryset.filter(
            Q(parent_code__icontains=value) |
            Q(child_code__icontains=value) |
            Q(location__icontains=value) |
            Q(kpo__icontains=value) |
            Q(stock__icontains=value) 
        ).distinct()
