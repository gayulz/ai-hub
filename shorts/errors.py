"""호출자가 구별할 수 있는 계약 오류."""


class TransitionError(ValueError):
	pass


class Conflict(ValueError):
	def __init__(self, message, *, owner=None):
		super().__init__(message)
		self.owner = owner


class StoreError(ValueError):
	pass
