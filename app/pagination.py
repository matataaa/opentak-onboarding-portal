"""Shared search, sorting and pagination for list endpoints"""
from flask import request
from sqlalchemy import func, or_


def search_and_paginate(query, sort_column, search_columns):
    """Apply ?search= (case-insensitive match on any of search_columns), sort case-insensitively on
    sort_column, then ?page= / ?per_page=. Without per_page every matching row is returned, so
    callers that need the full list (pickers, dashboards) keep working.

    Returns (items, meta) with meta = {'total', 'page', 'per_page'} for the JSON response.
    """
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', type=int)
    search = request.args.get('search', '')

    if search:
        search_filter = f'%{search}%'
        query = query.filter(or_(*(column.ilike(search_filter) for column in search_columns)))

    query = query.order_by(func.lower(sort_column))
    total = query.count()
    if per_page:
        query = query.offset((page - 1) * per_page).limit(per_page)

    return query.all(), {'total': total, 'page': page, 'per_page': per_page}
