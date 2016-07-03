import copy
import operator
from collections import OrderedDict, defaultdict

from cached_property import threaded_cached_property

from zeep.xsd.elements import Any, Base, Element, RefElement
from zeep.xsd.utils import UniqueAttributeName, max_occurs_iter
from zeep.xsd.valueobjects import CompoundValue

__all__ = ['All', 'Choice', 'Group', 'Sequence']


class Indicator(Base, list):
    name = None

    def __repr__(self):
        return '<%s(%s)>' % (
            self.__class__.__name__, super(Indicator, self).__repr__())

    def __init__(self, elements=None, min_occurs=1, max_occurs=1):
        self.min_occurs = min_occurs
        self.max_occurs = max_occurs

        if elements is None:
            super(Indicator, self).__init__()
        else:
            super(Indicator, self).__init__(elements)

    @threaded_cached_property
    def elements(self):
        """List of tuples containing the element name and the element"""
        result = []
        for name, elm in self.elements_nested:
            if name is None:
                result.extend(elm.elements)
            else:
                result.append((name, elm))
        return result

    @threaded_cached_property
    def elements_nested(self):
        """List of tuples containing the element name and the element"""
        result = []
        generator = UniqueAttributeName()
        for elm in self:
            if isinstance(elm, (All, Group, Sequence)):
                if elm.accepts_multiple:
                    result.append((generator.get_name(), elm))
                else:
                    result.append((None, elm))
            elif isinstance(elm, (Any, Choice)):
                result.append((generator.get_name(), elm))
            else:
                result.append((elm.name, elm))
        return result

    def accept(self, values):
        """Check if the current element accepts the given values."""
        required_keys = {
            name for name, element in self.elements
            if not element.is_optional
        }
        optional_keys = {
            name for name, element in self.elements
            if element.is_optional
        }

        values_keys = set(values)
        if '_xsd_elm' in values_keys:
            values_keys.remove('_xsd_elm')

        if (
            values_keys <= (required_keys | optional_keys) and
            required_keys <= values_keys
        ):
            return True
        return False

    def default_value(self):
        result = OrderedDict()
        for name, element in self.elements:

            # XXX: element.default_value
            if element.accepts_multiple:
                value = []
            else:
                value = None
            result[name] = value

        return result

    def parse_args(self, args):
        result = {}
        args = copy.copy(args)

        for name, element in self.elements:
            if not args:
                break
            arg = args.pop(0)
            result[name] = arg

        return result, args

    def parse_kwargs(self, kwargs, name=None):
        """Apply the given kwarg to the element.

        Returns a tuple with two dictionaries, the first one being the result
        and the second one the unparsed kwargs.

        """
        if self.accepts_multiple:
            assert name

        if name and name in kwargs:

            # Make sure we have a list, lame lame
            item_kwargs = kwargs.get(name)
            if not isinstance(item_kwargs, list):
                item_kwargs = [item_kwargs]

            result = []
            for i, item_value in zip(max_occurs_iter(self.max_occurs), item_kwargs):
                subresult = OrderedDict()
                for item_name, element in self.elements:
                    value, item_value = element.parse_kwargs(item_value, item_name)
                    if value is not None:
                        subresult.update(value)

                result.append(subresult)

            if self.accepts_multiple:
                result = {name: result}
            else:
                result = result[0] if result else None

            # All items consumed
            if not any(filter(None, item_kwargs)):
                del kwargs[name]

            return result, kwargs

        else:
            result = OrderedDict()
            for elm_name, element in self.elements:
                sub_result, kwargs = element.parse_kwargs(kwargs, elm_name)
                if sub_result is not None:
                    result.update(sub_result)

            if name:
                result = {name: result}

            return result, kwargs

    def resolve(self):
        for i, elm in enumerate(self):
            if isinstance(elm, RefElement):
                elm = elm.resolve()
            self[i] = elm
        return self

    def render(self, parent, value):
        if not isinstance(value, list):
            values = [value]
        else:
            values = value

        for i, value in zip(max_occurs_iter(self.max_occurs), values):
            for name, element in self.elements_nested:

                if name:
                    if isinstance(value, dict):
                        element_value = value.get(name)
                    else:
                        element_value = getattr(value, name, None)
                else:
                    element_value = value

                if element_value is not None or not element.is_optional:
                    element.render(parent, element_value)

    def signature(self, depth=0):
        depth += 1
        parts = []
        for name, element in self.elements_nested:
            if name:
                parts.append('%s: %s' % (name, element.signature(depth)))
            elif isinstance(element, Indicator):
                parts.append('%s' % (element.signature()))
            else:
                parts.append('%s: %s' % (name, element.signature(depth)))
        part = ', '.join(parts)

        if self.accepts_multiple:
            return '[%s]' % (part)
        return part


class All(Indicator):
    """Allows the elements in the group to appear (or not appear) in any order
    in the containing element.

    """

    def parse_xmlelements(self, xmlelements, schema, name=None):
        result = OrderedDict()

        values = defaultdict(list)
        for elm in xmlelements:
            values[elm.tag].append(elm)

        for name, element in self.elements:
            sub_elements = values.get(element.qname)
            if sub_elements:
                result[name] = element.parse_xmlelements(sub_elements, schema)

        return result


class Choice(Indicator):

    @property
    def is_optional(self):
        return True

    def default_value(self):
        return {}

    def parse_xmlelements(self, xmlelements, schema, name=None):
        result = []

        for i in max_occurs_iter(self.max_occurs):
            for node in list(xmlelements):

                # Choose out of multiple
                options = []
                for name, element in self.elements_nested:

                    local_xmlelements = copy.copy(xmlelements)
                    sub_result = element.parse_xmlelements(local_xmlelements, schema)

                    if isinstance(element, Indicator):
                        if element.accepts_multiple:
                            sub_result = {name: sub_result}
                    else:
                        sub_result = {name: sub_result}

                    num_consumed = len(xmlelements) - len(local_xmlelements)
                    if num_consumed:
                        options.append((num_consumed, sub_result))

                # Sort on least left
                options = sorted(options, key=operator.itemgetter(0))[::-1]
                if options:
                    result.append(options[0][1])
                    for i in range(options[0][0]):
                        xmlelements.pop(0)
                else:
                    break

        if not self.accepts_multiple:
            result = result[0] if result else None

        return result

    def parse_kwargs(self, kwargs, name):
        """Processes the kwargs for this choice element.

        Returns a tuple containing value, kwags.

        This handles two distinct initialization methods:

        1. Passing the choice elements directly to the kwargs (unnested)
        2. Passing the choice elements into the `name` kwarg (_alue_1) (nested).
           This case is required when multiple choice elements are given.

        :param name: Name of the choice element (_value_1)
        :type name: str
        :param element: Choice element object
        :type element: zeep.xsd.Choice
        :param kwargs: dict (or list of dicts) of kwargs for initialization
        :type kwargs: list / dict

        """
        result = []
        kwargs = copy.copy(kwargs)

        if name and name in kwargs:
            values = kwargs.pop(name)
            if isinstance(values, dict):
                values = [values]

            for value in values:
                for element in self:

                    # TODO: Use most greedy choice instead of first matching
                    if isinstance(element, Indicator):
                        choice_value = value[name] if name in value else value
                        if element.accept(choice_value):
                            result.append(choice_value)
                            break
                    else:
                        if element.name in value:
                            choice_value = value.get(element.name)
                            result.append({element.name: choice_value})
                            break
                else:
                    raise TypeError(
                        "No complete xsd:Sequence found for the xsd:Choice %r.\n"
                        "The signature is: %s" % (name, self.signature()))

            if not self.accepts_multiple:
                result = result[0] if result else None
        else:
            # Direct use-case isn't supported when maxOccurs > 1
            if self.accepts_multiple:
                return {}, kwargs

            # When choice elements are specified directly in the kwargs
            org_kwargs = kwargs
            for choice in self:
                result, kwargs = choice.parse_kwargs(org_kwargs)
                if result:
                    break
            else:
                result = {}
                kwargs = org_kwargs

        if name:
            result = {name: result}
        return result, kwargs

    def render(self, parent, value):
        if not self.accepts_multiple:
            value = [value]

        for item in value:

            # Find matching choice element
            for name, element in self.elements_nested:
                if isinstance(element, Element):
                    if element.name in item:
                        if isinstance(item, CompoundValue):
                            choice_value = getattr(item, element.name, item)
                        else:
                            choice_value = item.get(element.name, item)
                        element.render(parent, choice_value)
                        break
                else:
                    if name is not None:
                        if isinstance(item, CompoundValue):
                            choice_value = getattr(item, name, item)
                        else:
                            choice_value = item.get(name, item)
                    else:
                        choice_value = item

                    if element.accept(choice_value):
                        element.render(parent, choice_value)
                        break

    def signature(self, depth=0):
        parts = []
        for name, element in self.elements_nested:
            if isinstance(element, Indicator):
                parts.append('{%s}' % (element.signature(depth)))
            else:
                parts.append('{%s: %s}' % (name, element.signature(depth)))
        part = '(%s)' % ' | '.join(parts)
        if self.accepts_multiple:
            return '%s[]' % (part)
        return part


class Sequence(Indicator):

    def parse_xmlelements(self, xmlelements, schema, name=None):
        result = []
        for item in max_occurs_iter(self.max_occurs):
            item_result = OrderedDict()
            for elm_name, element in self.elements:
                item_result[elm_name] = element.parse_xmlelements(
                    xmlelements, schema)
                if not xmlelements:
                    break
            result.append(item_result)

        if not self.accepts_multiple:
            return result[0] if result else None
        return {name: result}


class Group(Base):
    """Groups a set of element declarations so that they can be incorporated as
    a group into complex type definitions.

    """

    def __init__(self, name, child, max_occurs=1, min_occurs=1):
        self.child = child
        self.qname = name
        self.name = name.localname
        self.max_occurs = max_occurs
        self.min_occurs = min_occurs

    def __iter__(self, *args, **kwargs):
        for item in self.child:
            yield item

    @threaded_cached_property
    def elements(self):
        if self.accepts_multiple:
            return [('_value_1', self.child)]
        return self.child.elements

    def default_value(self):
        result = OrderedDict()
        for name, element in self.elements:

            # XXX: element.default_value
            if element.accepts_multiple:
                value = []
            else:
                value = None
            result[name] = value

        return result

    def parse_args(self, args):
        return self.child.parse_args(args)

    def parse_kwargs(self, kwargs, name=None):
        if self.accepts_multiple:
            if name not in kwargs:
                return {}, kwargs

            item_kwargs = kwargs.pop(name)
            result = []
            sub_name = '_value_1' if self.child.accepts_multiple else None
            for i, sub_kwargs in zip(max_occurs_iter(self.max_occurs), item_kwargs):
                subresult, res_kwargs = self.child.parse_kwargs(sub_kwargs, sub_name)
                if subresult:
                    result.append(subresult)
            if result:
                result = {name: result}
        else:
            result, kwargs = self.child.parse_kwargs(kwargs, name)
        return result, kwargs

    def parse_xmlelements(self, xmlelements, schema, name=None):
        result = []

        for i in max_occurs_iter(self.max_occurs):
            result.append(
                self.child.parse_xmlelements(xmlelements, schema, name)
            )
        if not self.accepts_multiple and result:
            return result[0]
        return {name: result}

    def render(self, *args, **kwargs):
        return self.child.render(*args, **kwargs)

    def resolve(self):
        self.child = self.child.resolve()
        return self

    def signature(self, depth=0):
        return ''
