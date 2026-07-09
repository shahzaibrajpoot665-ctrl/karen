function updateURLParameter(param, value) {
    const url = new URL(window.location);
    url.searchParams.set(param, value);
    window.history.pushState({}, '', url);
}

function removeURLParameter(params) {
    const url = new URL(window.location);
    for (const param of params) {
        url.searchParams.delete(param);
    }
    window.history.pushState({}, '', url);
}
function updateURLParameters(params) {
    const url = new URL(window.location);
    for (const [param, value] of Object.entries(params)) {
        url.searchParams.set(param, value);
    }
    window.history.pushState({}, '', url);
}

function getURLParameter(param) {
    const urlParams = new URLSearchParams(window.location.search);
    return urlParams.get(param);
}

function ValidateAllFormFields(form_id,submit_btn_id){
    const form = document.getElementById(form_id);
    const submitBtn = document.getElementById(submit_btn_id);
    const inputs = form.querySelectorAll('input');

    const checkFormFields = () => {
        let allFilled = true;
        inputs.forEach(input => {
            if (input.value.trim() === '') {
                allFilled = false;
            }
        });
        submitBtn.disabled = !allFilled;
    };

    inputs.forEach(input => {
        input.addEventListener('input', checkFormFields);
    });
}


function formatNumberWithCommas(number) {
    return number.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function getFormattedQueryParams(excludeParams = []) {
    const url = window.location.href; // Get the current URL
    const urlObj = new URL(url);
    const params = new URLSearchParams(urlObj.search);
    const formattedParams = [];
  
    for (const [key, value] of params) {
      if (!excludeParams.includes(key)) {
        formattedParams.push(`${key}=${value}`);
      }
    }
    if (formattedParams.length > 0) {
        return '?' + formattedParams.join('&');        
    }else{
        return '';
    }
  }

function utilityFunction() {
    console.log('Utility function called');
}

function setPagination(paginationInfo, fetchDataFunction) {
    const { next, previous, total_pages, current_page } = paginationInfo;
    const paginationContainer = document.getElementById('pagination');

    // Clear existing pagination
    paginationContainer.innerHTML = '';

    // Helper function to create a pagination item
    function createPageItem(page, label = page, isActive = false) {
        const activeClass = isActive ? 'active' : '';
        const pageLi = document.createElement('li');
        pageLi.className = `page-item ${activeClass}`;
        pageLi.innerHTML = `<a href="#" class="page-link waves-effect" data-dt-idx="${page}">${label}</a>`;
        if (!isActive) {
            pageLi.addEventListener('click', (e) => {
                e.preventDefault();
                updateURLParameter('page', page);
                fetchDataFunction();
            });
        }
        return pageLi;
    }

    // Previous button
    if (previous !== null) {
        const firstPageLi = document.createElement('li');
        firstPageLi.className = 'page-item';
        firstPageLi.innerHTML = `<a class="page-link waves-effect" href="?page=1"><i class="mdi mdi-chevron-left"></i></a>`;
        firstPageLi.addEventListener('click', (e) => {
            e.preventDefault();
            updateURLParameter('page', 1);
            fetchDataFunction();
        });
        paginationContainer.appendChild(firstPageLi);

        const prevLi = document.createElement('li');
        prevLi.className = 'page-item';
        prevLi.innerHTML = `<a class="page-link waves-effect" href="?page=${current_page - 1}">previous</a>`;
        prevLi.addEventListener('click', (e) => {
            e.preventDefault();
            updateURLParameter('page', current_page - 1);
            fetchDataFunction();
        });
        paginationContainer.appendChild(prevLi);
    }

    // Determine start and end page numbers
    const startPage = Math.max(1, current_page - 3);
    const endPage = Math.min(total_pages, current_page + 3);

    // Handle case where there are more pages before the startPage
    if (startPage > 1) {
        paginationContainer.appendChild(createPageItem(1));
        if (startPage > 2) {
            const ellipsis = document.createElement('li');
            ellipsis.className = 'page-item disabled';
            ellipsis.innerHTML = `<a class="page-link waves-effect" href="#">...</a>`;
            paginationContainer.appendChild(ellipsis);
        }
    }

    // Page numbers
    for (let i = startPage; i <= endPage; i++) {
        paginationContainer.appendChild(createPageItem(i, i, i === current_page));
    }

    // Handle case where there are more pages after the endPage
    if (endPage < total_pages) {
        if (endPage < total_pages - 1) {
            const ellipsis = document.createElement('li');
            ellipsis.className = 'page-item disabled';
            ellipsis.innerHTML = `<a class="page-link waves-effect" href="#">...</a>`;
            paginationContainer.appendChild(ellipsis);
        }
        paginationContainer.appendChild(createPageItem(total_pages));
    }

    // Next button
    if (next !== null) {
        const nextLi = document.createElement('li');
        nextLi.className = 'page-item';
        nextLi.innerHTML = `<a class="page-link waves-effect" href="?page=${current_page + 1}">next</a>`;
        nextLi.addEventListener('click', (e) => {
            e.preventDefault();
            updateURLParameter('page', current_page + 1);
            fetchDataFunction();
        });
        paginationContainer.appendChild(nextLi);

        const lastPageLi = document.createElement('li');
        lastPageLi.className = 'page-item';
        lastPageLi.innerHTML = `<a class="page-link waves-effect" href="?page=${total_pages}"><i class="mdi mdi-chevron-right"></i></a>`;
        lastPageLi.addEventListener('click', (e) => {
            e.preventDefault();
            updateURLParameter('page', total_pages);
            fetchDataFunction();
        });
        paginationContainer.appendChild(lastPageLi);
    }
}


let sortDirection = 'asc';
let sortField = '';

function sortTable(field) {
    if (sortField === field) {
        sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
    } else {
        sortDirection = 'asc';
    }
    sortField = field;

    updateURLParameter('sort', sortField);
    updateURLParameter('direction', sortDirection);
    getData();
}