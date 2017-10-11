from typing import Optional

import flask
import graphene
import graphql.language.ast as graphql_ast
from flask import Response, request
from graphene.relay import Connection, Node
from graphene.test import default_format_error, format_execution_result
from graphene.types.definitions import GrapheneInterfaceType
from graphql import GraphQLEnumType, GraphQLObjectType, GraphQLScalarType, GraphQLNonNull, GraphQLList, GraphQLField
from graphql_server import encode_execution_results, json_encode


class GraphQLREST(object):
    schema: graphene.Schema
    app: flask.Flask = None

    def __init__(self, schema: graphene.Schema, app: flask.Flask = None):
        self.schema = schema

        if app is not None:
            self.init_app(app)

    def init_app(self, app: flask.Flask):
        self.app = app
        operations_list = (
            ('query', self.schema.get_query_type(), 'GET'),
            ('mutation', self.schema.get_mutation_type(), 'POST'),
        )

        for operation_name, operation_type, http_method in operations_list:
            for field_name, field in operation_type.fields.items():
                endpoint = f'{operation_name}.{operation_type.name}.{field_name}'
                app.add_url_rule(f'/{field_name}',
                                 view_func=self._get_view_func(operation_name, field, field_name),
                                 endpoint=endpoint,
                                 methods=[http_method, ])

    def format_result(self, result):
        return format_execution_result(result, default_format_error)

    def get_return_type(self, return_type):
        if isinstance(return_type, (GraphQLNonNull, GraphQLList)):
            return self.get_return_type(return_type.of_type)

        return return_type

    def get_variable_type(self, return_type):
        if isinstance(return_type, GraphQLNonNull):
            return graphql_ast.NonNullType(type=self.get_variable_type(return_type.of_type))
        elif isinstance(return_type, GraphQLList):
            return graphql_ast.ListType(type=self.get_variable_type(return_type.of_type))
        else:
            return graphql_ast.NamedType(name=graphql_ast.Name(value=return_type.name))

    def _get_field_selection_set(self,
                                 field: GraphQLField,
                                 include_connection: bool = True) -> Optional[graphql_ast.SelectionSet]:
        return_type = self.get_return_type(field.type)

        if isinstance(return_type, (GraphQLScalarType, GraphQLEnumType)):
            return None
        elif isinstance(return_type, (GraphQLObjectType, GrapheneInterfaceType)):
            all_selections = []

            if issubclass(return_type.graphene_type, Connection):
                if include_connection is False:
                    return None
                else:
                    include_connection = False

            for name, sub_field in return_type.fields.items():

                sub_field_type = self.get_return_type(sub_field.type)

                # check if nested connection should be included
                if hasattr(sub_field_type, 'graphene_type') and issubclass(sub_field_type.graphene_type,
                                                                           Connection) and include_connection is False:
                    continue

                selection = graphql_ast.Field(name=graphql_ast.Name(value=name),
                                              selection_set=self._get_field_selection_set(sub_field,
                                                                                          include_connection=include_connection))

                all_selections.append(selection)

            return graphql_ast.SelectionSet(
                selections=all_selections
            )

        raise NotImplementedError

    def _get_view_func(self, operation: str, field: graphene.Field, field_name: str):
        schema = self.schema
        variable_definitions = []
        arguments = []

        for arg_name, arg_definition in field.args.items():
            variable = graphql_ast.Variable(name=graphql_ast.Name(value=arg_name))
            arguments.append(graphql_ast.Argument(name=graphql_ast.Name(value=arg_name), value=variable))
            variable_type = self.get_variable_type(arg_definition.type)
            variable_definitions.append(graphql_ast.VariableDefinition(
                variable=variable,
                type=variable_type,
            ))

        def view_func():
            variable_values = self.get_variable_values()

            field_selection_set = self._get_field_selection_set(field, include_connection=True)

            if hasattr(field.type, 'graphene_type') and issubclass(field.type.graphene_type, Node):
                _type_name, _id = Node.from_global_id(variable_values['id'])
                node_type = schema.get_type(_type_name)
                inline_selection = graphql_ast.InlineFragment(
                    type_condition=graphql_ast.NamedType(name=graphql_ast.Name(value=_type_name)),
                    selection_set=self._get_field_selection_set(GraphQLField(node_type), include_connection=True)
                )
                field_selection_set.selections.append(inline_selection)

            document_ast = graphql_ast.Document(
                [
                    graphql_ast.OperationDefinition(
                        operation=operation,
                        variable_definitions=variable_definitions,
                        selection_set=graphql_ast.SelectionSet(
                            selections=[
                                graphql_ast.Field(
                                    name=graphql_ast.Name(value=field_name),
                                    arguments=arguments,
                                    selection_set=field_selection_set
                                )
                            ]
                        )
                    )
                ]
            )

            execution_results = schema.execute(
                document_ast,
                variable_values=variable_values
            )

            # TODO custom encoder that positions data[field_name] at data
            result, status_code = encode_execution_results([execution_results],
                                                           is_batch=False,
                                                           format_error=default_format_error,
                                                           encode=json_encode)

            return Response(result,
                            status=status_code,
                            content_type='application/json')

        return view_func

    def get_variable_values(self):
        if request.method == 'GET':
            return request.args
        elif request.method == 'POST':
            if request.content_type == 'application/json':
                return request.json
            else:
                return request.data

        raise NotImplementedError
